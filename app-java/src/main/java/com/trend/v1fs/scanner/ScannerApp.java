package com.trend.v1fs.scanner;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.core.ResponseBytes;
import software.amazon.awssdk.core.sync.RequestBody;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.cloudwatchlogs.CloudWatchLogsClient;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.CopyObjectRequest;
import software.amazon.awssdk.services.s3.model.DeleteObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectRequest;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Request;
import software.amazon.awssdk.services.s3.model.NoSuchKeyException;
import software.amazon.awssdk.services.s3.model.PutObjectRequest;
import software.amazon.awssdk.services.s3.model.PutObjectTaggingRequest;
import software.amazon.awssdk.services.s3.model.S3Object;
import software.amazon.awssdk.services.s3.model.Tag;
import software.amazon.awssdk.services.s3.model.Tagging;
import software.amazon.awssdk.services.s3.model.TaggingDirective;
import software.amazon.awssdk.services.secretsmanager.SecretsManagerClient;
import software.amazon.awssdk.services.secretsmanager.model.GetSecretValueRequest;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.ChangeMessageVisibilityRequest;
import software.amazon.awssdk.services.sqs.model.Message;
import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;

import java.net.InetAddress;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.Semaphore;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.regex.Pattern;

/**
 * S3 → SQS → V1FS scanning service. Java port of {@code app/scanner.py} with
 * full feature parity.
 *
 * <p>A poller thread long-polls SQS and hands each message to a worker pool;
 * concurrency is bounded by a count semaphore (MAX_CONCURRENT_SCANS) and a byte
 * budget (memory guard). Each message is parsed (S3 notification OR EventBridge
 * shape), each record is downloaded, scanned via gRPC, and routed:
 * malicious → quarantine · decompression-limit → review (or quarantine+tags
 * when review is off) · clean → tagged in place. Incompletely-scanned files are
 * never marked clean.
 */
public final class ScannerApp {

    private static final Logger log = LoggerFactory.getLogger(ScannerApp.class);

    /** foundErrors names that mean the scanner hit a decompression limit. */
    private static final Set<String> DECOMPRESSION_ERROR_NAMES = Set.of(
            "ATSE_ZIP_RATIO_ERR",       // compression ratio exceeded
            "ATSE_MAXDECOM_ERR",        // nesting depth exceeded
            "ATSE_ZIP_FILE_COUNT_ERR",  // file count exceeded
            "ATSE_EXTRACT_TOO_BIG_ERR"  // decompressed size exceeded
    );

    // S3 object-tag values allow letters, numbers, spaces, and + - = . _ : / @
    private static final Pattern TAG_DISALLOWED =
            Pattern.compile("[^\\w \\-=.:/@+]", Pattern.UNICODE_CHARACTER_CLASS);

    private final Config config;
    private final ObjectMapper mapper = new ObjectMapper();
    private final String hostname = resolveHostname();

    private final long maxFileSizeBytes;
    private final ByteBudget byteBudget;
    private final Semaphore concurrency;
    private final AtomicInteger inFlight = new AtomicInteger(0);

    private volatile boolean shutdownRequested = false;
    private volatile boolean ready = false;
    private int consecutiveErrors = 0;

    private S3Client s3;
    private SqsClient sqs;
    private SecretsManagerClient secrets;
    private CloudWatchLogsClient logsClient;

    private Dispatcher dispatcher;
    private DeleteBatcher deleteBatcher;
    private HealthServer healthServer;
    private AuditTrail auditTrail;

    private ExecutorService workers;
    private ScheduledExecutorService heartbeats;
    private Thread reconciliationThread;

    public ScannerApp(Config config) {
        this.config = config;
        this.maxFileSizeBytes = config.maxFileSizeBytes();
        // Floor the budget at one max-size file so a single large file never
        // deadlocks against its own budget.
        this.byteBudget = new ByteBudget(Math.max(config.maxInflightBytes, maxFileSizeBytes));
        this.concurrency = new Semaphore(config.maxConcurrentScans);
    }

    // --- Lifecycle -----------------------------------------------------------

    public void run() {
        try {
            start();
            pollLoop();
        } catch (Exception e) {
            log.error("Fatal error in scanner", e);
        } finally {
            shutdown();
        }
    }

    /** Ask the poll loop to stop; the caller drives the graceful drain. */
    public void requestShutdown() {
        shutdownRequested = true;
    }

    private void start() throws Exception {
        Region region = Region.of(config.awsRegion);
        s3 = S3Client.builder().region(region).build();
        sqs = SqsClient.builder().region(region).build();
        secrets = SecretsManagerClient.builder().region(region).build();
        if (!config.auditLogGroup.isEmpty()) {
            logsClient = CloudWatchLogsClient.builder().region(region).build();
        }

        log.info("Retrieving V1FS API key from Secrets Manager");
        String apiKey = secrets.getSecretValue(GetSecretValueRequest.builder()
                .secretId(config.v1fsApiKeySecretArn)
                .build()).secretString();

        if (config.dispatchMode.equals("pull")) {
            log.info("Dispatch mode: pull — discovering scanner pods from target group {}",
                    config.scannerTargetGroupArn);
            dispatcher = new ScannerPool(config, apiKey);
        } else {
            log.info("Dispatch mode: clusterip — initializing V1FS gRPC client at {} (tls={}, ca_cert={})",
                    config.v1fsServerAddr, config.v1fsTlsEnabled,
                    config.v1fsCaCert.isEmpty() ? "system" : config.v1fsCaCert);
            dispatcher = new ClusterIpDispatcher(config, apiKey);
        }

        deleteBatcher = new DeleteBatcher(sqs, config.sqsQueueUrl);
        deleteBatcher.start();

        workers = Executors.newCachedThreadPool(daemonFactory("scan-worker"));
        heartbeats = Executors.newScheduledThreadPool(2, daemonFactory("sqs-heartbeat"));

        ready = true;
        healthServer = new HealthServer(config.healthPort);
        healthServer.setReady(true);
        healthServer.start();

        if (!config.auditLogGroup.isEmpty()) {
            auditTrail = new AuditTrail(logsClient, config.auditLogGroup, hostname, config.auditQueueMaxSize);
            auditTrail.start();
        }

        log.info("Scanner started — polling {} (concurrency={}, pml={})",
                config.sqsQueueUrl, config.maxConcurrentScans, config.pmlEnabled);

        if (config.reconciliationEnabled) {
            reconciliationThread = new Thread(this::reconciliationLoop, "reconciliation");
            reconciliationThread.setDaemon(true);
            reconciliationThread.start();
            log.info("Reconciliation enabled — monitoring s3://{} every {}s for objects older than {}s",
                    config.reconciliationBucket, config.reconciliationInterval,
                    config.reconciliationAgeThreshold);
        }
    }

    private void pollLoop() {
        while (!shutdownRequested) {
            // Backpressure: pause polling when too many tasks are in-flight. The
            // 2x multiplier just limits the pending queue; the semaphore controls
            // actual concurrency.
            if (inFlight.get() >= config.maxConcurrentScans * 2) {
                sleep(100);
                continue;
            }

            List<Message> messages;
            try {
                messages = sqs.receiveMessage(ReceiveMessageRequest.builder()
                        .queueUrl(config.sqsQueueUrl)
                        .maxNumberOfMessages(10)
                        .waitTimeSeconds(20)
                        .build()).messages();
                consecutiveErrors = 0;
            } catch (Exception e) {
                consecutiveErrors++;
                // Exponential backoff capped at 60s, plus up to 1s of jitter.
                long backoffSec = Math.min(1L << Math.min(consecutiveErrors, 6), 60);
                long delayMs = backoffSec * 1000 + ThreadLocalRandom.current().nextInt(1000);
                log.error("SQS receive_message error, retrying in {}ms", delayMs, e);
                sleep(delayMs);
                continue;
            }

            for (Message msg : messages) {
                inFlight.incrementAndGet();
                workers.submit(() -> {
                    try {
                        concurrency.acquire();
                        try {
                            processMessage(msg);
                        } finally {
                            concurrency.release();
                        }
                    } catch (InterruptedException e) {
                        Thread.currentThread().interrupt();
                    } catch (Exception e) {
                        log.error("Unexpected error processing message {}", msg.messageId(), e);
                    } finally {
                        inFlight.decrementAndGet();
                    }
                });
            }
        }
    }

    private void shutdown() {
        ready = false;
        if (healthServer != null) healthServer.setReady(false);
        log.info("Shutting down — waiting for {} in-flight task(s)", inFlight.get());

        if (workers != null) {
            workers.shutdown();
            try {
                if (!workers.awaitTermination(60, TimeUnit.SECONDS)) {
                    log.warn("Worker pool did not drain within 60s");
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        // Flush pending SQS deletes before the client closes — all producers are
        // done now, so this drains every queued handle.
        if (deleteBatcher != null) deleteBatcher.stop();
        if (reconciliationThread != null) reconciliationThread.interrupt();
        if (heartbeats != null) heartbeats.shutdownNow();
        if (auditTrail != null) auditTrail.shutdown();
        if (healthServer != null) healthServer.stop();
        if (dispatcher != null) dispatcher.close();

        closeQuietly(s3);
        closeQuietly(sqs);
        closeQuietly(secrets);
        closeQuietly(logsClient);
        log.info("Shutdown complete");
    }

    // --- Message processing --------------------------------------------------

    private void processMessage(Message message) {
        String messageId = message.messageId() != null ? message.messageId() : "unknown";
        String receiptHandle = message.receiptHandle();
        if (receiptHandle == null) {
            log.error("Missing ReceiptHandle in SQS message [msg={}]", messageId);
            return;
        }

        long interval = Math.max(config.sqsVisibilityTimeout - 60, 30);
        ScheduledFuture<?> heartbeat = heartbeats.scheduleAtFixedRate(
                () -> extendVisibility(receiptHandle), interval, interval, TimeUnit.SECONDS);
        try {
            JsonNode body;
            try {
                body = mapper.readTree(message.body());
            } catch (Exception parse) {
                log.error("Malformed message body [msg={}], deleting", messageId, parse);
                deleteBatcher.add(receiptHandle);
                return;
            }

            List<S3Records.Record> records = S3Records.extract(body);
            if (records.isEmpty()) {
                // s3:TestEvent, non-Object-Created EventBridge event, or empty — discard
                log.info("No processable records in message {}, deleting", messageId);
                deleteBatcher.add(receiptHandle);
                return;
            }

            boolean allSucceeded = true;
            for (S3Records.Record record : records) {
                try {
                    processRecord(record, messageId);
                } catch (Exception e) {
                    log.error("Failed processing record s3://{}/{} [msg={}]",
                            record.bucket(), record.key(), messageId, e);
                    allSucceeded = false;
                }
            }

            if (allSucceeded) {
                deleteBatcher.add(receiptHandle);
            } else {
                log.warn("One or more records failed in message {} — shortening visibility for fast retry",
                        messageId);
                shortenVisibility(receiptHandle);
            }
        } catch (Exception e) {
            log.error("Failed processing message {} — shortening visibility for fast retry", messageId, e);
            shortenVisibility(receiptHandle);
        } finally {
            heartbeat.cancel(false);
        }
    }

    private void processRecord(S3Records.Record record, String messageId) throws Exception {
        String bucket = record.bucket();
        String key = record.key();
        long size = record.size();

        log.info("Processing s3://{}/{} ({} bytes) [msg={}]", bucket, key, size, messageId);

        // Oversize: server-side copy (no download), then finalize + audit.
        if (maxFileSizeBytes > 0 && size > maxFileSizeBytes) {
            String destBucket;
            String tag;
            String verdict;
            if (config.reviewRoutingEnabled) {
                destBucket = config.s3ReviewBucket;
                tag = "S3-Review-Oversize";
                verdict = "review";
                log.warn("OVERSIZE REVIEW: s3://{}/{} ({} > {} bytes), routing to review bucket via server-side copy",
                        bucket, key, size, maxFileSizeBytes);
            } else {
                destBucket = config.s3QuarantineBucket;
                tag = "S3-Oversize";
                verdict = "oversize";
                log.error("OVERSIZE: s3://{}/{} ({} > {} bytes), moving to quarantine via server-side copy",
                        bucket, key, size, maxFileSizeBytes);
            }
            copyObject(bucket, key, destBucket, Map.of("ScanResult", tag));
            finalizeSource(bucket, key, Map.of("ScanResult", tag));
            enqueueAudit(key, size, verdict, null, 0, messageId);
            return;
        }

        // Reserve the memory budget before pulling the file into RAM.
        long reserved = byteBudget.acquire(size);
        byte[] fileBytes;
        try {
            fileBytes = download(bucket, key);
        } catch (NoSuchKeyException nsk) {
            byteBudget.release(reserved);
            log.warn("Object s3://{}/{} no longer exists, skipping", bucket, key);
            return;
        } catch (Exception e) {
            byteBudget.release(reserved);
            throw e;
        }

        try {
            long scanStart = System.nanoTime();
            String resultJson = dispatcher.scan(fileBytes, key);
            int scanDurationMs = (int) ((System.nanoTime() - scanStart) / 1_000_000);
            JsonNode result = mapper.readTree(resultJson);

            boolean isMalicious = result.path("scanResult").asInt(0) > 0;
            List<String> decompressionErrors = getDecompressionErrors(result);

            Map<String, String> tags = new LinkedHashMap<>();
            String destBucket;
            String verdict;
            if (isMalicious) {
                destBucket = config.s3QuarantineBucket;
                verdict = "malicious";
                tags.put("ScanResult", "S3-Malware");
                log.warn("MALICIOUS: s3://{}/{} → s3://{}/{} sha256={} malware={}",
                        bucket, key, destBucket, key,
                        result.path("fileSHA256").asText("unknown"),
                        malwareNames(result));
            } else if (!decompressionErrors.isEmpty() && config.reviewRoutingEnabled) {
                destBucket = config.s3ReviewBucket;
                verdict = "review";
                tags.put("ScanResult", "S3-Review");
                log.warn("REVIEW: s3://{}/{} → s3://{}/{} (decompression limit errors: {})",
                        bucket, key, destBucket, key, decompressionErrors);
            } else if (!decompressionErrors.isEmpty()) {
                destBucket = config.s3QuarantineBucket;
                verdict = "quarantined-decompression-limit";
                tags.put("ScanResult", "S3-DecompressionLimit");
                // Dedupe and join with '-' (comma is not a legal S3 tag-value char).
                tags.put("ScanErrors", String.join("-", new LinkedHashSet<>(decompressionErrors)));
                log.warn("DECOMPRESSION LIMIT: s3://{}/{} → s3://{}/{} (errors: {}, "
                                + "review pipeline disabled — quarantining incompletely-scanned file)",
                        bucket, key, destBucket, key, decompressionErrors);
            } else {
                destBucket = null;   // clean files are left in place
                verdict = "clean";
                tags.put("ScanResult", "S3-Clean");
                log.info("CLEAN: s3://{}/{} (tagged in place)", bucket, key);
            }

            if (destBucket == null) {
                // Clean: leave the file where it is; just tag the source.
                tagSource(bucket, key, tags);
            } else {
                // Not fully clean: move to the verdict bucket, then finalize source.
                upload(destBucket, key, fileBytes, tags);
                finalizeSource(bucket, key, tags);
            }
            enqueueAudit(key, size, verdict, result, scanDurationMs, messageId);
        } finally {
            fileBytes = null;   // help GC release the large buffer promptly
            byteBudget.release(reserved);
        }
    }

    private static List<String> getDecompressionErrors(JsonNode result) {
        List<String> out = new ArrayList<>();
        for (JsonNode e : result.path("foundErrors")) {
            String name = e.path("name").asText("");
            if (DECOMPRESSION_ERROR_NAMES.contains(name)) out.add(name);
        }
        return out;
    }

    private List<String> malwareNames(JsonNode result) {
        List<String> out = new ArrayList<>();
        for (JsonNode m : result.path("foundMalwares")) {
            out.add(m.path("malwareName").asText(""));
        }
        return out;
    }

    // --- SQS visibility ------------------------------------------------------

    private void extendVisibility(String receiptHandle) {
        try {
            sqs.changeMessageVisibility(ChangeMessageVisibilityRequest.builder()
                    .queueUrl(config.sqsQueueUrl)
                    .receiptHandle(receiptHandle)
                    .visibilityTimeout(config.sqsVisibilityTimeout)
                    .build());
        } catch (Exception e) {
            log.warn("Failed to extend visibility", e);
        }
    }

    private void shortenVisibility(String receiptHandle) {
        try {
            sqs.changeMessageVisibility(ChangeMessageVisibilityRequest.builder()
                    .queueUrl(config.sqsQueueUrl)
                    .receiptHandle(receiptHandle)
                    .visibilityTimeout(30)
                    .build());
        } catch (Exception e) {
            log.debug("Failed to shorten visibility timeout", e);
        }
    }

    // --- S3 operations -------------------------------------------------------

    private byte[] download(String bucket, String key) {
        ResponseBytes<GetObjectResponse> resp = s3.getObjectAsBytes(
                GetObjectRequest.builder().bucket(bucket).key(key).build());
        return resp.asByteArray();
    }

    private void copyObject(String srcBucket, String key, String destBucket, Map<String, String> tags) {
        s3.copyObject(CopyObjectRequest.builder()
                .sourceBucket(srcBucket)
                .sourceKey(key)
                .destinationBucket(destBucket)
                .destinationKey(key)
                .tagging(taggingQuery(tags))
                .taggingDirective(TaggingDirective.REPLACE)
                .build());
    }

    private void upload(String bucket, String key, byte[] data, Map<String, String> tags) {
        PutObjectRequest.Builder req = PutObjectRequest.builder().bucket(bucket).key(key);
        if (tags != null && !tags.isEmpty()) {
            req.tagging(taggingQuery(tags));
        }
        s3.putObject(req.build(), RequestBody.fromBytes(data));
    }

    private void deleteObject(String bucket, String key) {
        s3.deleteObject(DeleteObjectRequest.builder().bucket(bucket).key(key).build());
    }

    /** Tag the source object in place with the verdict (no move, no delete). */
    private void tagSource(String bucket, String key, Map<String, String> tags) {
        List<Tag> tagSet = new ArrayList<>();
        for (Map.Entry<String, String> e : tags.entrySet()) {
            tagSet.add(Tag.builder().key(e.getKey()).value(safeTag(e.getValue())).build());
        }
        s3.putObjectTagging(PutObjectTaggingRequest.builder()
                .bucket(bucket).key(key)
                .tagging(Tagging.builder().tagSet(tagSet).build())
                .build());
    }

    /**
     * Finalize the source of a MOVED (not-clean) file: delete it when it is the
     * stack-owned ingest bucket (DELETE_SOURCE_ENABLED), else tag it in place so
     * a user's own object is annotated but never deleted.
     */
    private void finalizeSource(String bucket, String key, Map<String, String> tags) {
        if (config.deleteSourceEnabled) {
            deleteObject(bucket, key);
        } else {
            tagSource(bucket, key, tags);
        }
    }

    /** URL-encoded "k=v&..." tagging query, with values coerced to legal S3 tag values. */
    private static String taggingQuery(Map<String, String> tags) {
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : tags.entrySet()) {
            if (sb.length() > 0) sb.append('&');
            sb.append(urlEncode(e.getKey())).append('=').append(urlEncode(safeTag(e.getValue())));
        }
        return sb.toString();
    }

    /** Coerce a value into a legal S3 tag value (max 256 chars). */
    private static String safeTag(String value) {
        String cleaned = TAG_DISALLOWED.matcher(value).replaceAll("_");
        return cleaned.length() > 256 ? cleaned.substring(0, 256) : cleaned;
    }

    private static String urlEncode(String s) {
        return URLEncoder.encode(s, StandardCharsets.UTF_8);
    }

    // --- Audit ---------------------------------------------------------------

    private void enqueueAudit(String key, long size, String verdict, JsonNode result,
                              int scanDurationMs, String messageId) {
        if (auditTrail == null) return;
        Map<String, Object> entry = new LinkedHashMap<>();
        entry.put("timestamp", System.currentTimeMillis() / 1000.0);
        entry.put("file", key);
        entry.put("size", size);
        entry.put("verdict", verdict);
        entry.put("scanResult", result != null ? result.path("scanResult").asInt(-1) : -1);
        entry.put("sha256", result != null ? result.path("fileSHA256").asText("") : "");
        entry.put("malware", result != null ? malwareNames(result) : List.of());
        entry.put("foundErrors", result != null ? errorNames(result) : List.of());
        entry.put("scanId", result != null ? result.path("scanId").asText("") : "");
        entry.put("scannerVersion", result != null ? result.path("scannerVersion").asText("") : "");
        entry.put("fileSHA1", result != null ? result.path("fileSHA1").asText("") : "");
        entry.put("scanDurationMs", scanDurationMs);
        entry.put("pod", hostname);
        entry.put("messageId", messageId);
        auditTrail.enqueue(entry);
    }

    private List<String> errorNames(JsonNode result) {
        List<String> out = new ArrayList<>();
        for (JsonNode e : result.path("foundErrors")) {
            out.add(e.path("name").asText(""));
        }
        return out;
    }

    // --- Reconciliation ------------------------------------------------------

    private void reconciliationLoop() {
        String bucket = config.reconciliationBucket;
        String queueUrl = config.reconciliationQueueUrl;
        long threshold = config.reconciliationAgeThreshold;

        while (!shutdownRequested) {
            if (!sleep(config.reconciliationInterval * 1000L)) break;
            if (shutdownRequested) break;
            try {
                long now = System.currentTimeMillis() / 1000L;
                int requeued = 0;
                for (S3Object obj : s3.listObjectsV2Paginator(ListObjectsV2Request.builder()
                        .bucket(bucket).build()).contents()) {
                    long age = now - obj.lastModified().getEpochSecond();
                    if (age < threshold) continue;
                    String key = obj.key();
                    long size = obj.size();

                    // Synthetic S3 event notification to the main scan queue.
                    ObjectNode s3Node = mapper.createObjectNode();
                    s3Node.putObject("bucket").put("name", bucket);
                    ObjectNode objNode = s3Node.putObject("object");
                    objNode.put("key", quoteAll(key));
                    objNode.put("size", size);
                    ObjectNode rec = mapper.createObjectNode();
                    rec.put("eventSource", "aws:s3");
                    rec.put("eventName", "ObjectCreated:Reconciliation");
                    rec.set("s3", s3Node);
                    ObjectNode msg = mapper.createObjectNode();
                    msg.putArray("Records").add(rec);

                    sqs.sendMessage(SendMessageRequest.builder()
                            .queueUrl(queueUrl)
                            .messageBody(mapper.writeValueAsString(msg))
                            .build());
                    requeued++;
                }
                if (requeued > 0) {
                    log.warn("Reconciliation: re-queued {} orphaned files from s3://{} (age > {}s)",
                            requeued, bucket, threshold);
                } else {
                    log.debug("Reconciliation: no orphaned files in s3://{}", bucket);
                }
            } catch (Exception e) {
                log.warn("Reconciliation check failed", e);
            }
        }
    }

    /** Percent-encode every reserved char (Python urllib.parse.quote(key, safe="")). */
    private static String quoteAll(String key) {
        return URLEncoder.encode(key, StandardCharsets.UTF_8).replace("+", "%20");
    }

    // --- helpers -------------------------------------------------------------

    private static java.util.concurrent.ThreadFactory daemonFactory(String name) {
        AtomicInteger n = new AtomicInteger();
        return r -> {
            Thread t = new Thread(r, name + "-" + n.incrementAndGet());
            t.setDaemon(true);
            return t;
        };
    }

    /** Sleep; returns false if interrupted. */
    private static boolean sleep(long millis) {
        try {
            Thread.sleep(millis);
            return true;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    private static void closeQuietly(AutoCloseable c) {
        if (c == null) return;
        try {
            c.close();
        } catch (Exception ignored) {
            // best-effort at shutdown
        }
    }

    private static String resolveHostname() {
        String h = System.getenv("HOSTNAME");
        if (h != null && !h.isEmpty()) return h;
        try {
            return InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            return "unknown";
        }
    }
}
