import { readonly, ref, type Ref } from "vue";

export interface PollingVisibility {
  isVisible(): boolean;
  subscribe(listener: () => void): () => void;
}

type TimerHandle = ReturnType<typeof globalThis.setTimeout>;

export interface PollingTimers {
  setTimeout(callback: () => void, delay: number): TimerHandle;
  clearTimeout(handle: TimerHandle): void;
}

export interface UsePollingOptions {
  poll(signal: AbortSignal): Promise<void>;
  selectDelay(): number;
  timers?: PollingTimers;
  visibility?: PollingVisibility;
  onError?: (error: unknown) => void;
}

export interface PollingCoordinator {
  start(): void;
  stop(): void;
  triggerNow(): void;
  isRunning: Readonly<Ref<boolean>>;
}

const defaultTimers: PollingTimers = {
  setTimeout(callback, delay) {
    return globalThis.setTimeout(callback, delay);
  },
  clearTimeout(handle) {
    globalThis.clearTimeout(handle);
  },
};

function createDocumentVisibility(): PollingVisibility {
  return {
    isVisible() {
      return document.visibilityState !== "hidden";
    },
    subscribe(listener) {
      document.addEventListener("visibilitychange", listener);
      return () => {
        document.removeEventListener("visibilitychange", listener);
      };
    },
  };
}

export function usePolling(
  options: UsePollingOptions,
): PollingCoordinator {
  const timers = options.timers ?? defaultTimers;
  const visibility =
    options.visibility ?? createDocumentVisibility();
  const isRunning = ref(false);

  let generation = 0;
  let timer: TimerHandle | null = null;
  let inFlight = false;
  let triggerQueued = false;
  let controller: AbortController | null = null;
  let unsubscribeVisibility: (() => void) | null = null;

  function clearTimer(): void {
    if (timer !== null) {
      timers.clearTimeout(timer);
      timer = null;
    }
  }

  function scheduleNext(runGeneration: number): void {
    if (
      !isRunning.value ||
      runGeneration !== generation ||
      !visibility.isVisible()
    ) {
      return;
    }
    clearTimer();
    const delay = options.selectDelay();
    timer = timers.setTimeout(() => {
      timer = null;
      void runPoll(runGeneration);
    }, delay);
  }

  async function runPoll(runGeneration: number): Promise<void> {
    if (
      !isRunning.value ||
      runGeneration !== generation ||
      !visibility.isVisible()
    ) {
      return;
    }
    if (inFlight) {
      triggerQueued = true;
      return;
    }

    clearTimer();
    inFlight = true;
    controller = new AbortController();
    try {
      await options.poll(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) {
        options.onError?.(error);
      }
    } finally {
      controller = null;
      inFlight = false;

      if (!isRunning.value) {
        return;
      }
      if (runGeneration !== generation) {
        triggerQueued = false;
        if (visibility.isVisible()) {
          void runPoll(generation);
        }
        return;
      }
      if (!visibility.isVisible()) {
        triggerQueued = false;
        return;
      }
      if (triggerQueued) {
        triggerQueued = false;
        void runPoll(runGeneration);
        return;
      }
      scheduleNext(runGeneration);
    }
  }

  function triggerNow(): void {
    if (!isRunning.value || !visibility.isVisible()) {
      return;
    }
    clearTimer();
    if (inFlight) {
      triggerQueued = true;
      return;
    }
    void runPoll(generation);
  }

  function start(): void {
    if (isRunning.value) {
      return;
    }
    generation += 1;
    isRunning.value = true;
    unsubscribeVisibility = visibility.subscribe(() => {
      if (!visibility.isVisible()) {
        triggerQueued = false;
        clearTimer();
        return;
      }
      triggerNow();
    });
    triggerNow();
  }

  function stop(): void {
    generation += 1;
    isRunning.value = false;
    triggerQueued = false;
    clearTimer();
    controller?.abort();
    unsubscribeVisibility?.();
    unsubscribeVisibility = null;
  }

  return {
    start,
    stop,
    triggerNow,
    isRunning: readonly(isRunning),
  };
}
