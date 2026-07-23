package com.trend.v1fs.scanner;

import com.fasterxml.jackson.databind.JsonNode;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

/**
 * Normalizes the two S3 event shapes that arrive on the queue into
 * {@link Record}s — a faithful port of {@code scanner.py:_extract_records}.
 *
 * <ul>
 *   <li><b>S3 event notification</b> (stack-created bucket → SQS directly):
 *       {@code Records[*].s3.bucket.name / .object.key}. Keys are form-encoded
 *       (spaces as {@code +}), so they are decoded with unquote_plus semantics
 *       ({@link URLDecoder}).</li>
 *   <li><b>EventBridge "Object Created"</b> (existing user bucket → EventBridge
 *       rule → SQS): {@code detail.bucket.name / .object.key}. Keys are RAW and
 *       are NOT decoded — decoding would corrupt keys with literal {@code +} or
 *       {@code %}.</li>
 * </ul>
 */
public final class S3Records {

    private static final Logger log = LoggerFactory.getLogger(S3Records.class);

    /** One normalized S3 object reference. */
    public record Record(String bucket, String key, long size) { }

    private S3Records() { }

    public static List<Record> extract(JsonNode body) {
        List<Record> records = new ArrayList<>();
        if (body.has("Records")) {
            for (JsonNode rec : body.path("Records")) {
                JsonNode s3 = rec.path("s3");
                String bucket = text(s3.path("bucket").path("name"));
                String keyEncoded = text(s3.path("object").path("key"));
                if (isBlank(bucket) || isBlank(keyEncoded)) {
                    log.error("Malformed S3 record (missing bucket/key)");
                    continue;
                }
                records.add(new Record(
                        bucket,
                        unquotePlus(keyEncoded),
                        s3.path("object").path("size").asLong(0)));
            }
        } else if ("Object Created".equals(text(body.path("detail-type")))) {
            JsonNode detail = body.path("detail");
            String bucket = text(detail.path("bucket").path("name"));
            String key = text(detail.path("object").path("key"));
            if (isBlank(bucket) || isBlank(key)) {
                log.error("Malformed EventBridge S3 event (missing bucket/key)");
            } else {
                // EventBridge keys are raw — no decoding.
                records.add(new Record(bucket, key, detail.path("object").path("size").asLong(0)));
            }
        }
        return records;
    }

    /** Python urllib.parse.unquote_plus: '+' → space, %XX → byte. */
    private static String unquotePlus(String s) {
        return URLDecoder.decode(s, StandardCharsets.UTF_8);
    }

    private static String text(JsonNode n) {
        return n != null && !n.isMissingNode() && !n.isNull() ? n.asText() : null;
    }

    private static boolean isBlank(String s) {
        return s == null || s.isEmpty();
    }
}
