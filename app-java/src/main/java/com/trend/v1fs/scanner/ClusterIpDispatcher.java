package com.trend.v1fs.scanner;

import com.trend.cloudone.amaas.AMaasClient;
import com.trend.cloudone.amaas.AMaasScanOptions;

/**
 * DISPATCH_MODE=clusterip: one long-lived {@link AMaasClient} to the in-cluster
 * Service / NLB / ALB endpoint (V1FS_SERVER_ADDR). The single gRPC channel is
 * thread-safe and multiplexes all concurrent scans — the SDK's documented reuse
 * pattern. Mirrors the clusterip branch of {@code scanner.py:start / _scan}.
 */
public final class ClusterIpDispatcher implements Dispatcher {

    private final AMaasClient client;
    private final boolean pml;

    public ClusterIpDispatcher(Config cfg, String apiKey) throws Exception {
        // 6-arg constructor: (region, host, apiKey, timeoutSecs, enableTLS, caCert).
        // region is null for a self-hosted endpoint. caCert null uses the system
        // trust store; a PEM path trusts a self-signed ALB cert (there is no
        // skip-verify option in the SDK).
        String caCert = cfg.v1fsCaCert == null || cfg.v1fsCaCert.isEmpty() ? null : cfg.v1fsCaCert;
        this.client = new AMaasClient(
                null,
                cfg.v1fsServerAddr,
                apiKey,
                cfg.scanTimeoutSecs,
                cfg.v1fsTlsEnabled,
                caCert);
        this.pml = cfg.pmlEnabled;
    }

    @Override
    public String scan(byte[] data, String uid) throws Exception {
        AMaasScanOptions options = AMaasScanOptions.builder()
                .pml(pml)
                .tagList(new String[]{"S3-Scan"})
                .build();
        // scanBuffer(data, identifier, digest, options). digest=true so repeat
        // scans of the same content hit the scanner's hash cache.
        return client.scanBuffer(data, uid, true, options);
    }

    @Override
    public void close() {
        try {
            client.close();
        } catch (Exception ignored) {
            // channel teardown is best-effort at shutdown
        }
    }
}
