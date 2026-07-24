import { afterEach, describe, expect, it, vi } from "vitest";

import {
  usePolling,
  type PollingVisibility,
} from "../../src/composables/usePolling";

class ControlledVisibility implements PollingVisibility {
  visible = true;
  private readonly listeners = new Set<() => void>();

  isVisible(): boolean {
    return this.visible;
  }

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  setVisible(visible: boolean): void {
    this.visible = visible;
    for (const listener of this.listeners) {
      listener();
    }
  }
}

function deferred<T = void>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function flushAsync(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
});

describe("usePolling", () => {
  it("polls immediately when started", async () => {
    const poll = vi.fn<(signal: AbortSignal) => Promise<void>>(() =>
      Promise.resolve(),
    );
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
    });

    polling.start();
    await flushAsync();

    expect(poll).toHaveBeenCalledOnce();
    expect(poll.mock.calls[0]?.[0]).toBeInstanceOf(AbortSignal);
    polling.stop();
  });

  it("waits 15000 ms after a stable poll", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
    });
    polling.start();
    await flushAsync();

    await vi.advanceTimersByTimeAsync(14_999);
    expect(poll).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("waits 2000 ms after an active poll", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 2_000,
    });
    polling.start();
    await flushAsync();

    await vi.advanceTimersByTimeAsync(1_999);
    expect(poll).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("uses recursive timeouts and never setInterval", async () => {
    vi.useFakeTimers();
    const intervalSpy = vi.spyOn(globalThis, "setInterval");
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 2_000,
    });

    polling.start();
    await vi.advanceTimersByTimeAsync(6_000);

    expect(poll).toHaveBeenCalledTimes(4);
    expect(intervalSpy).not.toHaveBeenCalled();
    polling.stop();
  });

  it("does not overlap when a poll remains unresolved", async () => {
    vi.useFakeTimers();
    const first = deferred();
    const poll = vi.fn(() => first.promise);
    const polling = usePolling({
      poll,
      selectDelay: () => 2_000,
    });
    polling.start();

    await vi.advanceTimersByTimeAsync(60_000);
    expect(poll).toHaveBeenCalledOnce();

    first.resolve();
    await flushAsync();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("queues triggerNow behind the current poll", async () => {
    const first = deferred();
    const poll = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValue(undefined);
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
    });
    polling.start();

    polling.triggerNow();
    polling.triggerNow();
    expect(poll).toHaveBeenCalledOnce();

    first.resolve();
    await flushAsync();
    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("clears its scheduled timeout when stopped", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 2_000,
    });
    polling.start();
    await flushAsync();

    polling.stop();
    await vi.advanceTimersByTimeAsync(20_000);

    expect(poll).toHaveBeenCalledOnce();
    expect(polling.isRunning.value).toBe(false);
  });

  it("aborts the current GET when stopped", () => {
    const signals: AbortSignal[] = [];
    const poll = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return new Promise<void>(() => undefined);
    });
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
    });
    polling.start();

    polling.stop();

    expect(signals[0]?.aborted).toBe(true);
  });

  it("does not schedule while hidden", async () => {
    vi.useFakeTimers();
    const visibility = new ControlledVisibility();
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 2_000,
      visibility,
    });
    polling.start();
    await flushAsync();

    visibility.setVisible(false);
    await vi.advanceTimersByTimeAsync(20_000);

    expect(poll).toHaveBeenCalledOnce();
    polling.stop();
  });

  it("polls immediately when the page becomes visible", async () => {
    const visibility = new ControlledVisibility();
    visibility.visible = false;
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
      visibility,
    });
    polling.start();
    expect(poll).not.toHaveBeenCalled();

    visibility.setVisible(true);
    await flushAsync();

    expect(poll).toHaveBeenCalledOnce();
    polling.stop();
  });

  it("runs only one recovery poll after a queued trigger is hidden", async () => {
    const visibility = new ControlledVisibility();
    const first = deferred();
    const poll = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockResolvedValue(undefined);
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
      visibility,
    });
    polling.start();
    polling.triggerNow();
    expect(poll).toHaveBeenCalledOnce();

    visibility.setVisible(false);
    first.resolve();
    await flushAsync();
    visibility.setVisible(true);
    await flushAsync();

    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("uses the latest delay when state changes between polls", async () => {
    vi.useFakeTimers();
    let active = false;
    const poll = vi.fn(async () => {
      active = !active;
    });
    const polling = usePolling({
      poll,
      selectDelay: () => (active ? 2_000 : 15_000),
    });
    polling.start();
    await flushAsync();

    await vi.advanceTimersByTimeAsync(2_000);
    expect(poll).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(poll).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(13_000);
    expect(poll).toHaveBeenCalledTimes(3);
    polling.stop();
  });

  it("schedules only one normal delay after a poll error", async () => {
    vi.useFakeTimers();
    const poll = vi
      .fn()
      .mockRejectedValueOnce(new Error("read failed"))
      .mockResolvedValue(undefined);
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
    });
    polling.start();
    await flushAsync();

    await vi.advanceTimersByTimeAsync(14_999);
    expect(poll).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("treats repeated start calls as one coordinator", async () => {
    vi.useFakeTimers();
    const poll = vi.fn(() => Promise.resolve());
    const polling = usePolling({
      poll,
      selectDelay: () => 2_000,
    });

    polling.start();
    polling.start();
    polling.start();
    await flushAsync();

    expect(poll).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(poll).toHaveBeenCalledTimes(2);
    polling.stop();
  });

  it("aborts an old conversation request before a restarted poll", async () => {
    const first = deferred();
    const signals: AbortSignal[] = [];
    const poll = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return signals.length === 1 ? first.promise : Promise.resolve();
    });
    const polling = usePolling({
      poll,
      selectDelay: () => 15_000,
    });
    polling.start();

    polling.stop();
    polling.start();
    expect(signals[0]?.aborted).toBe(true);
    expect(poll).toHaveBeenCalledOnce();

    first.resolve();
    await flushAsync();
    expect(poll).toHaveBeenCalledTimes(2);
    expect(signals[1]?.aborted).toBe(false);
    polling.stop();
  });
});
