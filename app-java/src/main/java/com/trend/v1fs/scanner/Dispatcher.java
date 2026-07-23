package com.trend.v1fs.scanner;

/**
 * Abstracts the two ways the app reaches the V1FS scanner (DISPATCH_MODE):
 * a single long-lived client to a Service/LB (clusterip) or the per-pod pull
 * pool (pull). Either way {@link #scan} returns the raw V1FS JSON verdict, so
 * routing stays dispatch-agnostic — mirrors {@code scanner.py:_scan}.
 */
public interface Dispatcher extends AutoCloseable {

    /** Scan a buffer and return the V1FS JSON verdict string. */
    String scan(byte[] data, String uid) throws Exception;

    @Override
    void close();
}
