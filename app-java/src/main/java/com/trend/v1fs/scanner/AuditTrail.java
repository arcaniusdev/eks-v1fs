package com.trend.v1fs.scanner;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import software.amazon.awssdk.services.cloudwatchlogs.CloudWatchLogsClient;
import software.amazon.awssdk.services.cloudwatchlogs.model.CreateLogStreamRequest;
import software.amazon.awssdk.services.cloudwatchlogs.model.InputLogEvent;
import software.amazon.awssdk.services.cloudwatchlogs.model.PutLogEventsRequest;
import software.amazon.awssdk.services.cloudwatchlogs.model.ResourceAlreadyExistsException;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * CloudWatch Logs audit trail — port of the audit flusher in {@code scanner.py}.
 *
 * <p>One structured JSON line per scan is enqueued and a background thread
 * batches up to 25 entries (sorted by timestamp) into {@code PutLogEvents}.
 * If the log group/stream is missing the trail degrades gracefully (logged,
 * never fatal). Enqueue drops (with an error log) when the bounded queue is
 * full so scanning is never blocked by audit backpressure.
 */
public final class AuditTrail {

    private static final Logger log = LoggerFactory.getLogger(AuditTrail.class);
    private static final int MAX_BATCH = 25;

    private final CloudWatchLogsClient logs;
    private final String logGroup;
    private final String streamName;
    private final int maxSize;
    private final ObjectMapper mapper = new ObjectMapper();
    private final BlockingQueue<Map<String, Object>> queue;
    private volatile boolean shuttingDown = false;
    private Thread worker;

    public AuditTrail(CloudWatchLogsClient logs, String logGroup, String streamName, int maxSize) {
        this.logs = logs;
        this.logGroup = logGroup;
        this.streamName = streamName;
        this.maxSize = maxSize;
        this.queue = new ArrayBlockingQueue<>(maxSize);
    }

    public void start() {
        worker = new Thread(this::run, "audit-flusher");
        worker.setDaemon(true);
        worker.start();
    }

    /** Enqueue an audit entry; drop (with an error) if the queue is full. */
    public void enqueue(Map<String, Object> entry) {
        if (!queue.offer(entry)) {
            log.error("Audit queue full ({} entries), dropping entry for {}",
                    maxSize, entry.get("file"));
        }
    }

    /** Signal shutdown; the flush loop drains the remaining queue before exiting. */
    public void shutdown() {
        shuttingDown = true;
        if (worker != null) {
            try {
                worker.join(TimeUnit.SECONDS.toMillis(10));
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
    }

    private void run() {
        try {
            logs.createLogStream(CreateLogStreamRequest.builder()
                    .logGroupName(logGroup)
                    .logStreamName(streamName)
                    .build());
        } catch (ResourceAlreadyExistsException ok) {
            // stream already exists — fine
        } catch (Exception e) {
            log.error("Failed to create audit log stream", e);
            return;
        }
        log.info("Audit trail: {}/{}", logGroup, streamName);

        while (!shuttingDown || !queue.isEmpty()) {
            List<Map<String, Object>> batch = new ArrayList<>();
            try {
                Map<String, Object> first = queue.poll(1, TimeUnit.SECONDS);
                if (first == null) continue;
                batch.add(first);
                while (batch.size() < MAX_BATCH) {
                    Map<String, Object> next = queue.poll();
                    if (next == null) break;
                    batch.add(next);
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            flush(batch);
        }
    }

    private void flush(List<Map<String, Object>> batch) {
        if (batch.isEmpty()) return;
        try {
            List<InputLogEvent> events = new ArrayList<>(batch.size());
            for (Map<String, Object> e : batch) {
                double ts = ((Number) e.get("timestamp")).doubleValue();
                events.add(InputLogEvent.builder()
                        .timestamp((long) (ts * 1000))
                        .message(mapper.writeValueAsString(e))
                        .build());
            }
            // PutLogEvents requires events in chronological order.
            events.sort(Comparator.comparingLong(InputLogEvent::timestamp));
            logs.putLogEvents(PutLogEventsRequest.builder()
                    .logGroupName(logGroup)
                    .logStreamName(streamName)
                    .logEvents(events)
                    .build());
        } catch (Exception e) {
            log.warn("Failed to write {} audit entries", batch.size(), e);
        }
    }
}
