package com.trend.v1fs.scanner;

import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;

/**
 * A semaphore over a byte budget — the port of {@code scanner.py:ByteBudget}.
 *
 * <p>Gates in-memory work so the total bytes held across concurrent scans stays
 * under a limit, protecting the pod from OOM when many large files arrive at
 * once (a bound the plain count semaphore, MAX_CONCURRENT_SCANS, can't give).
 * A request larger than the whole budget is clamped so it still runs (alone,
 * when it reaches the front). A total of 0 disables gating.
 */
public final class ByteBudget {

    private final long total;
    private long available;
    private final ReentrantLock lock = new ReentrantLock();
    private final Condition cond = lock.newCondition();

    public ByteBudget(long total) {
        this.total = total;
        this.available = total;
    }

    /** Reserve up to {@code n} bytes (clamped to the total); returns the amount reserved. */
    public long acquire(long n) throws InterruptedException {
        if (total <= 0) return 0;
        long want = Math.max(0, Math.min(n, total));
        lock.lock();
        try {
            while (available < want) {
                cond.await();
            }
            available -= want;
        } finally {
            lock.unlock();
        }
        return want;
    }

    public void release(long n) {
        if (total <= 0 || n <= 0) return;
        lock.lock();
        try {
            available += n;
            cond.signalAll();
        } finally {
            lock.unlock();
        }
    }
}
