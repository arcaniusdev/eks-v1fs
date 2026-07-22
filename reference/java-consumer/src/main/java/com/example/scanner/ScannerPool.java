package com.example.scanner;

import com.trend.cloudone.amaas.AMaasClient;
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
 * Client-side "pull" load balancer over the V1FS scanner pods.
 *
 * <p>Discovers the live, HEALTHY scanner pod IPs from the NLB target group
 * (created with target-type=ip) via the ELB {@code DescribeTargetHealth} API,
 * holds one {@link AMaasClient} (== one reused gRPC connection) per pod, and
 * hands each scan to the pod with the most free capacity (least-outstanding).
 *
 * <p>The NLB is used ONLY as a discovery registry — scans connect DIRECTLY to
 * pod IPs, so there is no load balancer in the scan path (no L4 connection
 * pinning, no L7 latency). A background thread re-reads the target group every
 * 20s so the pool tracks the KEDA-scaled fleet and drains pods on scale-down.
 */
public final class ScannerPool implements AutoCloseable {

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
    private final boolean tls;
    private final String caCertPath;    // null for plaintext (in-VPC NLB default)
    private final Map<String, PodClient> pods = new ConcurrentHashMap<>();
    private final ScheduledExecutorService refresher =
            Executors.newSingleThreadScheduledExecutor(r -> {
                Thread t = new Thread(r, "scanner-pool-refresh");
                t.setDaemon(true);
                return t;
            });

    public ScannerPool(ElasticLoadBalancingV2Client elb, String targetGroupArn,
                       String apiKey, int perPodCapacity, boolean tls, String caCertPath) {
        this.elb = elb;
        this.targetGroupArn = targetGroupArn;
        this.apiKey = apiKey;
        this.perPodCapacity = perPodCapacity;
        this.tls = tls;
        this.caCertPath = caCertPath;
        reconcile();  // initial roster before we start serving
        refresher.scheduleWithFixedDelay(this::reconcileQuietly, 20, 20, TimeUnit.SECONDS);
    }

    /** Total scan slots across all healthy pods — size your worker pool near this. */
    public int totalCapacity() {
        return pods.size() * perPodCapacity;
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
        // Add a client for each newly-appeared pod.
        for (String addr : healthy) {
            pods.computeIfAbsent(addr, a -> new PodClient(a, newClient(a), perPodCapacity));
        }
        // Drain + close pods that left the target group (scaled down / unhealthy).
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
            // region == null for a self-hosted endpoint; host is "<podIP>:50051".
            // caCertPath is null for the plaintext in-VPC NLB (the default here).
            return new AMaasClient(null, hostPort, apiKey, 300, tls, caCertPath);
        } catch (Exception e) {
            throw new RuntimeException("AMaasClient init failed for " + hostPort, e);
        }
    }

    /**
     * Scan a buffer against the least-busy scanner pod, retrying on a DIFFERENT
     * pod if the chosen one errors. Returns the V1FS JSON verdict.
     *
     * @throws NoCapacityException if no pod frees a slot within {@code waitMillis}
     *         — the caller should nack so the source queue redelivers (outer
     *         competing-consumers safety net).
     */
    public String scan(byte[] data, String uid, long waitMillis) throws Exception {
        Exception last = null;
        for (int attempt = 0; attempt < 3; attempt++) {
            PodClient pc = acquireLeastBusy(waitMillis);
            if (pc == null) throw new NoCapacityException("no scanner capacity within " + waitMillis + "ms");
            try {
                // NOTE: the exact scanBuffer signature varies by SDK version
                // (tags/PML flags or an AMaasScanOptions builder) — adjust to
                // your file-security-java-sdk version's javadoc.
                return pc.client.scanBuffer(data, uid, new String[]{}, false);
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

    private void reconcileQuietly() { try { reconcile(); } catch (Exception ignored) { } }

    private void closeQuietly(PodClient pc) {
        // AMaasClient holds a gRPC channel — close it to avoid leaking connections.
        try { pc.client.close(); } catch (Exception ignored) { }
    }

    @Override public void close() {
        refresher.shutdownNow();
        pods.values().forEach(this::closeQuietly);
        pods.clear();
    }

    /** Thrown when every scanner pod is saturated — signal the queue to redeliver. */
    public static final class NoCapacityException extends Exception {
        public NoCapacityException(String m) { super(m); }
    }
}
