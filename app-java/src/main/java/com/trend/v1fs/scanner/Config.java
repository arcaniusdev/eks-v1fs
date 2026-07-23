package com.trend.v1fs.scanner;

import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/**
 * Configuration loaded from environment variables — a faithful port of
 * {@code app/config.py}. Every variable name, default, and validation rule
 * mirrors the Python reference so the two implementations are interchangeable.
 */
public final class Config {

    private static final Set<String> VALID_LOG_LEVELS =
            Set.of("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL");

    // Required
    public final String sqsQueueUrl;
    public final String s3QuarantineBucket;
    public final String v1fsApiKeySecretArn;
    public final String awsRegion;

    // Optional / informational
    public final String s3IngestBucket;
    public final String s3ReviewBucket;

    // Scanner endpoint / dispatch
    public final String v1fsServerAddr;
    public final boolean v1fsTlsEnabled;
    public final String v1fsCaCert;
    public final String dispatchMode;             // clusterip | pull
    public final String scannerTargetGroupArn;
    public final int perPodCapacity;
    public final int podRefreshSecs;
    public final long scanTimeoutSecs;

    // Behavior
    public final String logLevel;
    public final int maxConcurrentScans;
    public final boolean pmlEnabled;
    public final String auditLogGroup;
    public final int healthPort;
    public final int maxFileSizeMb;
    public final long maxInflightBytes;
    public final boolean reviewRoutingEnabled;
    public final boolean deleteSourceEnabled;
    public final int sqsVisibilityTimeout;
    public final int auditQueueMaxSize;

    // Reconciliation
    public final boolean reconciliationEnabled;
    public final String reconciliationBucket;
    public final String reconciliationQueueUrl;
    public final int reconciliationInterval;
    public final int reconciliationAgeThreshold;

    private Config(Builder b) {
        this.sqsQueueUrl = b.sqsQueueUrl;
        this.s3QuarantineBucket = b.s3QuarantineBucket;
        this.v1fsApiKeySecretArn = b.v1fsApiKeySecretArn;
        this.awsRegion = b.awsRegion;
        this.s3IngestBucket = b.s3IngestBucket;
        this.s3ReviewBucket = b.s3ReviewBucket;
        this.v1fsServerAddr = b.v1fsServerAddr;
        this.v1fsTlsEnabled = b.v1fsTlsEnabled;
        this.v1fsCaCert = b.v1fsCaCert;
        this.dispatchMode = b.dispatchMode;
        this.scannerTargetGroupArn = b.scannerTargetGroupArn;
        this.perPodCapacity = b.perPodCapacity;
        this.podRefreshSecs = b.podRefreshSecs;
        this.scanTimeoutSecs = b.scanTimeoutSecs;
        this.logLevel = b.logLevel;
        this.maxConcurrentScans = b.maxConcurrentScans;
        this.pmlEnabled = b.pmlEnabled;
        this.auditLogGroup = b.auditLogGroup;
        this.healthPort = b.healthPort;
        this.maxFileSizeMb = b.maxFileSizeMb;
        this.maxInflightBytes = b.maxInflightBytes;
        this.reviewRoutingEnabled = b.reviewRoutingEnabled;
        this.deleteSourceEnabled = b.deleteSourceEnabled;
        this.sqsVisibilityTimeout = b.sqsVisibilityTimeout;
        this.auditQueueMaxSize = b.auditQueueMaxSize;
        this.reconciliationEnabled = b.reconciliationEnabled;
        this.reconciliationBucket = b.reconciliationBucket;
        this.reconciliationQueueUrl = b.reconciliationQueueUrl;
        this.reconciliationInterval = b.reconciliationInterval;
        this.reconciliationAgeThreshold = b.reconciliationAgeThreshold;
    }

    /** Total downloaded bytes allowed in memory across concurrent scans. */
    public long maxFileSizeBytes() {
        return (long) maxFileSizeMb * 1024 * 1024;
    }

    private static String env(String name, String def) {
        String v = System.getenv(name);
        return v != null ? v : def;
    }

    /** Parse an integer env var with inclusive bounds validation. */
    private static long intEnv(String name, String def, long min, long max) {
        String raw = env(name, def);
        long val;
        try {
            val = Long.parseLong(raw.trim());
        } catch (NumberFormatException e) {
            throw new IllegalArgumentException(name + " must be an integer, got: '" + raw + "'");
        }
        if (val < min || val > max) {
            throw new IllegalArgumentException(name + " must be " + min + "-" + max + ", got: " + val);
        }
        return val;
    }

    private static boolean boolEnv(String name, String def) {
        return env(name, def).toLowerCase().equals("true");
    }

    /** Load and validate configuration from the process environment. */
    public static Config load() {
        Builder b = new Builder();

        String sqsQueueUrl = System.getenv("SQS_QUEUE_URL");
        String s3QuarantineBucket = System.getenv("S3_QUARANTINE_BUCKET");
        String v1fsApiKeySecretArn = System.getenv("V1FS_API_KEY_SECRET_ARN");
        String awsRegion = System.getenv("AWS_REGION");

        List<String> missing = new ArrayList<>();
        if (isBlank(sqsQueueUrl)) missing.add("SQS_QUEUE_URL");
        if (isBlank(s3QuarantineBucket)) missing.add("S3_QUARANTINE_BUCKET");
        if (isBlank(v1fsApiKeySecretArn)) missing.add("V1FS_API_KEY_SECRET_ARN");
        if (isBlank(awsRegion)) missing.add("AWS_REGION");
        if (!missing.isEmpty()) {
            throw new IllegalArgumentException(
                    "Missing required environment variables: " + String.join(", ", missing));
        }
        b.sqsQueueUrl = sqsQueueUrl;
        b.s3QuarantineBucket = s3QuarantineBucket;
        b.v1fsApiKeySecretArn = v1fsApiKeySecretArn;
        b.awsRegion = awsRegion;

        // Informational only — the source bucket comes from each SQS message.
        b.s3IngestBucket = env("S3_INGEST_BUCKET", "");

        b.reviewRoutingEnabled = boolEnv("REVIEW_ROUTING_ENABLED", "true");
        b.s3ReviewBucket = env("S3_REVIEW_BUCKET", "");
        if (b.reviewRoutingEnabled && isBlank(b.s3ReviewBucket)) {
            throw new IllegalArgumentException(
                    "S3_REVIEW_BUCKET is required when REVIEW_ROUTING_ENABLED is true");
        }

        b.dispatchMode = env("DISPATCH_MODE", "clusterip").toLowerCase();
        if (!b.dispatchMode.equals("clusterip") && !b.dispatchMode.equals("pull")) {
            throw new IllegalArgumentException(
                    "DISPATCH_MODE must be 'clusterip' or 'pull', got: '" + b.dispatchMode + "'");
        }
        b.scannerTargetGroupArn = env("SCANNER_TARGET_GROUP_ARN", "");
        if (b.dispatchMode.equals("pull") && isBlank(b.scannerTargetGroupArn)) {
            throw new IllegalArgumentException(
                    "SCANNER_TARGET_GROUP_ARN is required when DISPATCH_MODE=pull");
        }

        b.logLevel = env("LOG_LEVEL", "INFO").toUpperCase();
        if (!VALID_LOG_LEVELS.contains(b.logLevel)) {
            throw new IllegalArgumentException(
                    "Invalid LOG_LEVEL: '" + b.logLevel + "'. Must be one of " + VALID_LOG_LEVELS);
        }
        configureLogging(b.logLevel);

        b.v1fsServerAddr = env("V1FS_SERVER_ADDR", "my-release-visionone-filesecurity-scanner:50051");
        b.v1fsTlsEnabled = boolEnv("V1FS_TLS_ENABLED", "false");
        b.v1fsCaCert = env("V1FS_CA_CERT", "");
        b.perPodCapacity = (int) intEnv("PER_POD_CAPACITY", "30", 1, 1000);
        b.podRefreshSecs = (int) intEnv("POD_REFRESH_SECS", "20", 5, 300);
        // The Java SDK takes the per-scan deadline in the constructor; the Python
        // SDK reads TM_AM_SCAN_TIMEOUT_SECS from the environment. Honor the same
        // variable here so the deadline is consistent across implementations.
        b.scanTimeoutSecs = intEnv("TM_AM_SCAN_TIMEOUT_SECS", "300", 1, 43200);

        b.maxConcurrentScans = (int) intEnv("MAX_CONCURRENT_SCANS", "50", 1, 1000);
        b.pmlEnabled = boolEnv("PML_ENABLED", "false");
        b.auditLogGroup = env("AUDIT_LOG_GROUP", "");
        b.healthPort = (int) intEnv("HEALTH_PORT", "8080", 1, 65535);
        b.maxFileSizeMb = (int) intEnv("MAX_FILE_SIZE_MB", "500", 0, 4096);
        b.maxInflightBytes = intEnv("MAX_INFLIGHT_MB", "1024", 0, 65536) * 1024 * 1024;
        b.deleteSourceEnabled = boolEnv("DELETE_SOURCE_ENABLED", "true");
        b.sqsVisibilityTimeout = (int) intEnv("SQS_VISIBILITY_TIMEOUT", "600", 30, 43200);
        b.auditQueueMaxSize = (int) intEnv("AUDIT_QUEUE_MAX_SIZE", "1000", 100, 100000);

        b.reconciliationEnabled = boolEnv("RECONCILIATION_ENABLED", "false");
        b.reconciliationBucket = env("RECONCILIATION_BUCKET", "");
        b.reconciliationQueueUrl = env("RECONCILIATION_QUEUE_URL", "");
        b.reconciliationInterval = (int) intEnv("RECONCILIATION_INTERVAL", "300", 60, 3600);
        b.reconciliationAgeThreshold = (int) intEnv("RECONCILIATION_AGE_THRESHOLD", "1800", 300, 86400);

        return new Config(b);
    }

    private static boolean isBlank(String s) {
        return s == null || s.isEmpty();
    }

    /** Map the Python log-level name onto slf4j-simple's level property. */
    private static void configureLogging(String level) {
        String slf4j = switch (level) {
            case "DEBUG" -> "debug";
            case "WARNING" -> "warn";
            case "ERROR", "CRITICAL" -> "error";
            default -> "info";
        };
        System.setProperty("org.slf4j.simpleLogger.defaultLogLevel", slf4j);
        System.setProperty("org.slf4j.simpleLogger.showDateTime", "true");
        System.setProperty("org.slf4j.simpleLogger.dateTimeFormat", "yyyy-MM-dd HH:mm:ss");
        System.setProperty("org.slf4j.simpleLogger.showThreadName", "false");
    }

    private static final class Builder {
        String sqsQueueUrl, s3QuarantineBucket, v1fsApiKeySecretArn, awsRegion;
        String s3IngestBucket, s3ReviewBucket;
        String v1fsServerAddr, v1fsCaCert, dispatchMode, scannerTargetGroupArn;
        boolean v1fsTlsEnabled;
        int perPodCapacity, podRefreshSecs;
        long scanTimeoutSecs;
        String logLevel, auditLogGroup;
        int maxConcurrentScans, healthPort, maxFileSizeMb, sqsVisibilityTimeout, auditQueueMaxSize;
        long maxInflightBytes;
        boolean pmlEnabled, reviewRoutingEnabled, deleteSourceEnabled;
        boolean reconciliationEnabled;
        String reconciliationBucket, reconciliationQueueUrl;
        int reconciliationInterval, reconciliationAgeThreshold;
    }
}
