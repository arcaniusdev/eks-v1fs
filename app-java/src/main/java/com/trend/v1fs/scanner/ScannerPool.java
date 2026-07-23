package com.trend.v1fs.scanner;

import com.trend.cloudone.amaas.AMaasClient;
import com.trend.cloudone.amaas.AMaasScanOptions;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.elasticloadbalancingv2.ElasticLoadBalancingV2Client;
import software.amazon.awssdk.services.elasticloadbalancingv2.model.DescribeTargetHealthRequest;
import software.amazon.awssdk.services.elasticloadbalancingv2.model.TargetHealthStateEnum;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

/**
 * DISPATCH_MODE=pull: client-side pull dispatcher over the V1FS scanner pods —
 * the Java port of {@code scanner.py:AsyncPodPool}, adapted from the working
 * reference at {@code reference/java-KEDA}.
 *
 * <p>Discovers the live, HEALTHY scanner pod IPs from the NLB target group
 * (target-type=ip) via the ELB {@code DescribeTargetHealth} API, holds one
 * {@link AMaasClient} (== one reused gRPC connection) per pod, and hands each
 * scan to the pod with the most free capacity (least-outstanding). The NLB is
 * only a discovery registry — scans connect DIRECTLY to pod IPs, so no load
 * balancer sits in the scan path. A background thread re-reads the target group
 * every POD_REFRESH_SECS so the pool tracks the KEDA-scaled fleet and drains
 * pods on scale-down.
 */
public final class ScannerPool implements Dispatcher {

    private static final Logger log = LoggerFactory.getLogger(ScannerPool.class);
    private static final long ACQUIRE_TIMEOUT_MS = 60_000;   // matches the Python 60s deadline

    /** One scanner pod: its SDK client and a capacity semaphore ("free slots"). */
    private static final class PodClient {
        final String addr;              // "10.2.x.y:50051"
        final AMaasClient client;
        final Semaphore slots;
        volatile boolean draining = false;
        PodClient(String addr, AMaasClient client, int capacity) {
            this.addr = addr;
            this.client = client;
            this.slots = new Semaphore(capacity);
        }
    }

    private final ElasticLoadBalancingV2Client elb;
    private final String targetGroupArn;
    private final String apiKey;
    private final int perPodCapacity;
    private final long timeoutSecs;
    private final boolean tls;
    private final String caCertPath;    // null for plaintext (in-VPC NLB default)
    private final boolean pml;
    private final Map<String, PodClient> pods = new ConcurrentHashMap<>();
    private final ScheduledExecutorService refresher =
            Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "scanner-pool-refresh");
                t.setDaemon(true);
                return t;
            });

    public ScannerPool(Config cfg, String apiKey) {
        this.elb = ElasticLoadBalancingV2Client.builder()
                .region(Region.of(cfg.awsRegion))
                .build();
        this.targetGroupArn = cfg.scannerTargetGroupArn;
        this.apiKey = apiKey;
        this.perPodCapacity = cfg.perPodCapacity;
        this.timeoutSecs = cfg.scanTimeoutSecs;
        // Pull mode connects to raw pod IPs; a cert SAN can't match a pod IP, so
        // it is normally plaintext. Still honor the configured TLS settings to
        // match the Python AsyncPodPool.
        this.tls = cfg.v1fsTlsEnabled;
        this.caCertPath = cfg.v1fsCaCert == null || cfg.v1fsCaCert.isEmpty() ? null : cfg.v1fsCaCert;
        this.pml = cfg.pmlEnabled;
        reconcile();  // seed the roster before serving
        log.info("Pull dispatcher started — {} scanner pod(s) discovered from target group",
                pods.size());
        refresher.scheduleWithFixedDelay(this::reconcileQuietly,
                cfg.podRefreshSecs, cfg.podRefreshSecs, TimeUnit.SECONDS);
    }

    /** Refresh the pod set from the target group's HEALTHY targets. */
    private void reconcile() {
        var resp = elb.describeTargetHealth(
                DescribeTargetHealthRequest.builder().targetGroupArn(targetGroupArn).build());
        Set<String> healthy = new HashSet<>();
        for (var d : resp.targetHealthDescriptions()) {
            if (d.targetHealth().state() == TargetHealthStateEnum.HEALTHY) {
                healthy.add(d.target().id() + ":" + d.target().port());
            }
        }
        for (String addr : healthy) {
            pods.computeIfAbsent(addr, a -> new PodClient(a, newClient(a), perPodCapacity));
        }
        for (String addr : new ArrayList<>(pods.keySet())) {
            if (!healthy.contains(addr)) {
                PodClient pc = pods.remove(addr);
                if (pc != null) {
                    pc.draining = true;   // stop new dispatch; in-flight scans finish
                    closeQuietly(pc);
                }
            }
        }
    }

    private AMaasClient newClient(String hostPort) {
        try {
            // (region, host, apiKey, timeoutSecs, enableTLS, caCert). region null
            // for a self-hosted endpoint; host is "<podIP>:50051".
            return new AMaasClient(null, hostPort, apiKey, timeoutSecs, tls, caCertPath);
        } catch (Exception e) {
            throw new RuntimeException("AMaasClient init failed for " + hostPort, e);
        }
    }

    /**
     * Scan against the least-busy scanner pod, retrying on a DIFFERENT pod if the
     * chosen one errors. Returns the V1FS JSON verdict.
     */
    @Override
    public String scan(byte[] data, String uid) throws Exception {
        AMaasScanOptions options = AMaasScanOptions.builder()
                .pml(pml)
                .tagList(new String[]{"S3-Scan"})
                .build();
        Exception last = null;
        for (int attempt = 0; attempt < 3; attempt++) {
            PodClient pc = acquireLeastBusy(ACQUIRE_TIMEOUT_MS);
            if (pc == null) {
                throw new NoCapacityException(
                        "no scanner pod capacity within " + ACQUIRE_TIMEOUT_MS + "ms");
            }
            try {
                return pc.client.scanBuffer(data, uid, true, options);
            } catch (Exception e) {
                last = e;                       // pod-level failure → try another pod
                if (pc.draining) pods.remove(pc.addr);
            } finally {
                pc.slots.release();
            }
        }
        throw last != null ? last : new IllegalStateException("scan failed after retries");
    }

    /** Pick the pod with the most free slots and take one; block until one frees. */
    private PodClient acquireLeastBusy(long waitMillis) throws InterruptedException {
        long deadline = System.currentTimeMillis() + Math.max(0, waitMillis);
        do {
            PodClient best = null;
            int bestFree = -1;
            for (PodClient pc : pods.values()) {
                if (pc.draining) continue;
                int free = pc.slots.availablePermits();
                if (free > bestFree) { bestFree = free; best = pc; }
            }
            if (best != null && best.slots.tryAcquire(200, TimeUnit.MILLISECONDS)) return best;
        } while (System.currentTimeMillis() < deadline);
        return null;
    }

    private void reconcileQuietly() {
        try {
            reconcile();
        } catch (Exception e) {
            log.warn("Pod discovery refresh failed; keeping current roster", e);
        }
    }

    private void closeQuietly(PodClient pc) {
        try { pc.client.close(); } catch (Exception ignored) { }
    }

    @Override
    public void close() {
        refresher.shutdownNow();
        pods.values().forEach(this::closeQuietly);
        pods.clear();
        try { elb.close(); } catch (Exception ignored) { }
    }

    /** Thrown when every scanner pod is saturated — leave the message for redelivery. */
    public static final class NoCapacityException extends Exception {
        public NoCapacityException(String m) { super(m); }
    }
}
