package com.trend.v1fs.scanner;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/**
 * Kubelet probe endpoint on HEALTH_PORT — port of the async health server in
 * {@code scanner.py}. {@code /healthz} (liveness) always returns 200 once the
 * server is up; {@code /readyz} (readiness) returns 200 when the scan
 * client/pool is initialized and 503 during startup/shutdown. Uses the JDK's
 * built-in {@link HttpServer} (no external dependency).
 */
public final class HealthServer {

    private static final Logger log = LoggerFactory.getLogger(HealthServer.class);

    private final int port;
    private final AtomicBoolean ready = new AtomicBoolean(false);
    private HttpServer server;

    public HealthServer(int port) {
        this.port = port;
    }

    public void setReady(boolean r) {
        ready.set(r);
    }

    public void start() throws IOException {
        server = HttpServer.create(new InetSocketAddress("0.0.0.0", port), 0);
        server.createContext("/healthz", ex -> respond(ex, 200, "ok"));
        server.createContext("/readyz", ex -> {
            if (ready.get()) {
                respond(ex, 200, "ready");
            } else {
                respond(ex, 503, "not ready");
            }
        });
        server.createContext("/", ex -> respond(ex, 404, "not found"));
        server.setExecutor(Executors.newFixedThreadPool(2, r -> {
            Thread t = new Thread(r, "health-server");
            t.setDaemon(true);
            return t;
        }));
        server.start();
        log.info("Health server listening on port {}", port);
    }

    public void stop() {
        if (server != null) {
            server.stop(0);
        }
    }

    private static void respond(HttpExchange ex, int code, String body) {
        try {
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            ex.sendResponseHeaders(code, bytes.length);
            try (OutputStream os = ex.getResponseBody()) {
                os.write(bytes);
            }
        } catch (IOException e) {
            log.debug("Health request handler error", e);
        } finally {
            ex.close();
        }
    }
}
