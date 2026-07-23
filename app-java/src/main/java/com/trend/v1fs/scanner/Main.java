package com.trend.v1fs.scanner;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Entrypoint. Loads configuration, starts the scanner on its own thread, and
 * installs a JVM shutdown hook for graceful SIGTERM/SIGINT handling: the hook
 * asks the poll loop to stop and blocks until the app has drained in-flight
 * work, flushed audit, and closed clients — mirroring the signal handling in
 * {@code scanner.py:main}.
 */
public final class Main {

    private static final Logger log = LoggerFactory.getLogger(Main.class);

    public static void main(String[] args) {
        Config config;
        try {
            config = Config.load();
        } catch (RuntimeException e) {
            System.err.println("Configuration error: " + e.getMessage());
            System.exit(2);
            return;
        }

        ScannerApp app = new ScannerApp(config);
        CountDownLatch done = new CountDownLatch(1);

        Thread appThread = new Thread(() -> {
            try {
                app.run();
            } finally {
                done.countDown();
            }
        }, "scanner-main");
        appThread.setDaemon(false);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            log.info("Received termination signal — draining");
            app.requestShutdown();
            try {
                // Give the app time to drain in-flight scans (k8s grace period).
                done.await(35, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }, "shutdown-hook"));

        appThread.start();
        try {
            appThread.join();
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
