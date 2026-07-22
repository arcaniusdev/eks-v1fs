package com.example.scanner;

import software.amazon.awssdk.core.SdkBytes;
import software.amazon.awssdk.services.elasticloadbalancingv2.ElasticLoadBalancingV2Client;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.DeleteMessageRequest;
import software.amazon.awssdk.services.sqs.model.Message;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;

import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Reference SQS-driven scanner consumer.
 *
 * <p>This is the "pull / competing-consumers" pattern the customer's queue-fed
 * gateway can use to keep the scanner-facing leg self-balancing WITHOUT a load
 * balancer in the path (no L4 hot-spot, no L7 latency):
 *
 * <ol>
 *   <li>A fixed pool of workers long-poll the same SQS queue — they COMPETE for
 *       messages, so work flows to whichever worker is free (outer pull).</li>
 *   <li>Each worker hands its scan to the scanner pod with the most free
 *       capacity via {@link ScannerPool} (inner pull, least-outstanding).</li>
 *   <li>The message is deleted (acked) only after a successful scan+action; any
 *       failure leaves it for SQS to redeliver (reliability net #1). A pod-level
 *       failure is retried on a different pod inside the pool (net #2).</li>
 * </ol>
 *
 * <p>Config via env: SCAN_QUEUE_URL, SCANNER_TARGET_GROUP_ARN, V1FS_API_KEY,
 * WORKERS (default = totalCapacity), PER_POD_CAPACITY (default 30),
 * SCANNER_TLS (default false), SCANNER_CA_CERT (path, optional).
 */
public final class Main {

    // S3 event notification record: pull bucket + (form-encoded) key.
    private static final Pattern BUCKET = Pattern.compile("\"name\"\\s*:\\s*\"([^\"]+)\"");
    private static final Pattern KEY = Pattern.compile("\"key\"\\s*:\\s*\"([^\"]+)\"");

    public static void main(String[] args) throws Exception {
        String queueUrl = env("SCAN_QUEUE_URL");
        String tgArn = env("SCANNER_TARGET_GROUP_ARN");
        String apiKey = env("V1FS_API_KEY");
        int perPod = Integer.parseInt(System.getenv().getOrDefault("PER_POD_CAPACITY", "30"));
        boolean tls = Boolean.parseBoolean(System.getenv().getOrDefault("SCANNER_TLS", "false"));
        String caCert = System.getenv("SCANNER_CA_CERT");   // null → system/plaintext

        SqsClient sqs = SqsClient.create();
        S3Client s3 = S3Client.create();
        ElasticLoadBalancingV2Client elb = ElasticLoadBalancingV2Client.create();

        try (ScannerPool pool = new ScannerPool(elb, tgArn, apiKey, perPod, tls, caCert)) {
            // Size the worker pool near total scanner capacity so the fleet can
            // be saturated but not overcommitted; the per-pod semaphores cap it.
            int workers = Integer.parseInt(System.getenv()
                    .getOrDefault("WORKERS", String.valueOf(Math.max(1, pool.totalCapacity()))));
            System.out.printf("Starting %d workers against %d scanner slots%n",
                    workers, pool.totalCapacity());
            for (int i = 0; i < workers; i++) {
                Thread t = new Thread(() -> workerLoop(sqs, s3, pool, queueUrl), "scan-worker-" + i);
                t.start();
            }
            Thread.currentThread().join();   // run until killed (SIGTERM)
        }
    }

    private static void workerLoop(SqsClient sqs, S3Client s3, ScannerPool pool, String queueUrl) {
        while (!Thread.currentThread().isInterrupted()) {
            var recv = sqs.receiveMessage(ReceiveMessageRequest.builder()
                    .queueUrl(queueUrl).maxNumberOfMessages(1).waitTimeSeconds(20).build());
            for (Message m : recv.messages()) {
                try {
                    handle(s3, pool, m);
                    // ACK: delete only after a successful scan + action.
                    sqs.deleteMessage(DeleteMessageRequest.builder()
                            .queueUrl(queueUrl).receiptHandle(m.receiptHandle()).build());
                } catch (Exception e) {
                    // NACK by omission: leave the message; SQS redelivers after
                    // the visibility timeout (net #1). Log and move on.
                    System.err.println("scan failed, leaving for redelivery: " + e.getMessage());
                }
            }
        }
    }

    private static void handle(S3Client s3, ScannerPool pool, Message m) throws Exception {
        String body = m.body();
        Matcher bm = BUCKET.matcher(body), km = KEY.matcher(body);
        if (!bm.find() || !km.find()) throw new IllegalArgumentException("no S3 ref in message");
        String bucket = bm.group(1);
        // S3 event keys are form-encoded (spaces as '+') — decode like the Python app.
        String key = URLDecoder.decode(km.group(1).replace("+", "%20"), StandardCharsets.UTF_8);

        byte[] data = s3.getObjectAsBytes(GetObjectRequest.builder()
                .bucket(bucket).key(key).build()).asByteArray();

        // Inner pull: least-busy pod, retry-on-another, backpressure if saturated.
        String verdict = pool.scan(data, key, /*waitMillis*/ 60_000);
        act(bucket, key, verdict);
    }

    /** Replace with the customer's real action (route/tag/alert on the verdict). */
    private static void act(String bucket, String key, String verdictJson) {
        boolean malware = verdictJson.contains("\"scanResult\":1");
        System.out.printf("%s/%s -> %s%n", bucket, key, malware ? "MALWARE" : "clean");
    }

    private static String env(String name) {
        String v = System.getenv(name);
        if (v == null || v.isBlank()) throw new IllegalStateException("missing env: " + name);
        return v;
    }
}
