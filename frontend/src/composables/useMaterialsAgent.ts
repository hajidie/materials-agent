import {
  computed,
  getCurrentScope,
  onScopeDispose,
  readonly,
  ref,
  type DeepReadonly,
  type Ref,
} from "vue";

import {
  createMaterialsAgentApi,
  type MaterialsAgentApi,
} from "../api/client";
import {
  ApiResponseError,
  ConversationCreationUncertaintyError,
  RequestAbortedError,
  toUserVisibleError,
  type UserVisibleError,
} from "../api/errors";
import type {
  ApiSuccessEnvelope,
  Conversation,
  ConversationListItem,
  ExplanationRetryRequest,
  TaskDetail,
  TimelineItem,
  ToolRetryRequest,
} from "../api/types";
import {
  useIdempotentRequest,
  type MutationOperation,
  type MutationRequestBody,
  type MutationStatus,
  type NewTaskMutationBody,
  type PendingMutationV1,
  type SupplementMutationBody,
} from "./useIdempotentRequest";
import { usePolling } from "./usePolling";
import {
  useFirstTurnOperation,
  type FirstTurnDescriptorV1,
} from "./useFirstTurnOperation";

export interface SupplementTarget {
  conversationId: string;
  taskId: string;
  summary: string;
}

export interface UseMaterialsAgentOptions {
  api?: MaterialsAgentApi;
  storage?: Storage;
  keyFactory?: () => string;
}

export interface CompleteTimeline {
  items: TimelineItem[];
  lastRequestId: string;
}

const REFRESH_AFTER_WRITE_MESSAGE =
  "操作已成功，但界面刷新失败。请手动刷新以查看最新状态。";

export interface MaterialsAgentState {
  conversations: DeepReadonly<Ref<ConversationListItem[]>>;
  conversationNextCursor: Readonly<Ref<string | null>>;
  selectedConversationId: Readonly<Ref<string | null>>;
  timeline: DeepReadonly<Ref<TimelineItem[]>>;
  timelineLoading: Readonly<Ref<boolean>>;
  conversationListLoading: Readonly<Ref<boolean>>;
  taskDetailsById: DeepReadonly<
    Ref<Record<string, TaskDetail>>
  >;
  taskDetailsLoadingById: DeepReadonly<
    Ref<Record<string, boolean>>
  >;
  supplementTarget: DeepReadonly<Ref<SupplementTarget | null>>;
  pendingMutation: DeepReadonly<
    Ref<PendingMutationV1 | FirstTurnDescriptorV1 | null>
  >;
  mutationStatus: Readonly<Ref<MutationStatus>>;
  conversationCreationUncertain: Readonly<Ref<boolean>>;
  firstTurnDraft: Readonly<Ref<string>>;
  globalError: DeepReadonly<Ref<UserVisibleError | null>>;
  globalErrors: DeepReadonly<Ref<UserVisibleError[]>>;
  lastRequestId: Readonly<Ref<string | null>>;
  initialize(): Promise<void>;
  loadConversations(reset?: boolean): Promise<void>;
  refreshConversations(): Promise<void>;
  loadMoreConversations(): Promise<void>;
  createConversation(title?: string): Promise<Conversation>;
  showBlankWorkspace(): void;
  deleteConversation(conversationId: string): Promise<void>;
  selectConversation(conversationId: string): Promise<void>;
  refreshTimeline(): Promise<void>;
  loadTaskHistory(taskId: string): Promise<TaskDetail>;
  setSupplementTarget(target: SupplementTarget): void;
  cancelSupplementTarget(): void;
  submitNewTask(contentText: string): Promise<void>;
  submitSupplement(contentText: string): Promise<void>;
  retryTool(taskId: string): Promise<void>;
  retryExplanation(
    resultId: string,
    language?: string,
  ): Promise<void>;
  retryPendingMutation(): Promise<void>;
  discardPendingMutation(): { discarded: boolean };
  startPolling(): void;
  stopPolling(): void;
}

export async function loadCompleteTimeline(
  api: MaterialsAgentApi,
  conversationId: string,
  signal: AbortSignal,
): Promise<CompleteTimeline> {
  const items: TimelineItem[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  let lastRequestId = "";

  while (true) {
    const response = await api.getTimelinePage(
      conversationId,
      50,
      cursor,
      signal,
    );
    lastRequestId = response.request_id;
    items.push(...response.data.items);
    if (!response.data.has_more) {
      return { items, lastRequestId };
    }
    const nextCursor = response.data.next_cursor;
    if (nextCursor === null) {
      throw new Error("Timeline 分页游标缺失。");
    }
    if (seenCursors.has(nextCursor)) {
      throw new Error("Timeline 分页游标重复。");
    }
    seenCursors.add(nextCursor);
    cursor = nextCursor;
  }
}

export function useMaterialsAgent(
  options: UseMaterialsAgentOptions = {},
): MaterialsAgentState {
  const api = options.api ?? createMaterialsAgentApi();
  const conversations = ref<ConversationListItem[]>([]);
  const conversationNextCursor = ref<string | null>(null);
  const selectedConversationId = ref<string | null>(null);
  const timeline = ref<TimelineItem[]>([]);
  const timelineLoading = ref(false);
  const conversationListLoading = ref(false);
  const taskDetailsById = ref<Record<string, TaskDetail>>({});
  const taskDetailsLoadingById = ref<Record<string, boolean>>({});
  const supplementTarget = ref<SupplementTarget | null>(null);
  const actionError = ref<UserVisibleError | null>(null);
  const readError = ref<UserVisibleError | null>(null);
  const conversationCreationUncertain = ref(false);
  const firstTurn = useFirstTurnOperation({
    api,
    ...(options.storage === undefined
      ? {}
      : { storage: options.storage }),
    ...(options.keyFactory === undefined
      ? {}
      : { keyFactory: options.keyFactory }),
  });
  const firstTurnDraft = computed(
    () => firstTurn.pending.value?.originalDraft ?? "",
  );
  const conversationCreationUncertaintyError =
    computed<UserVisibleError | null>(() =>
      conversationCreationUncertain.value
        ? toUserVisibleError(
            new ConversationCreationUncertaintyError(),
          )
        : null,
    );
  const firstTurnRecoveryError = computed<UserVisibleError | null>(
    () =>
      firstTurn.pending.value === null
        ? null
        : {
            message:
              "首条消息尚未完整确认。请使用原请求继续重试；此操作不能放弃。",
          },
  );
  const globalErrors = computed<UserVisibleError[]>(() => {
    const candidates = [
      conversationCreationUncertaintyError.value,
      firstTurnRecoveryError.value,
      actionError.value,
      readError.value,
    ];
    const seen = new Set<string>();
    const errors: UserVisibleError[] = [];
    for (const candidate of candidates) {
      if (candidate === null) {
        continue;
      }
      const key = JSON.stringify([
        candidate.message,
        candidate.status ?? null,
        candidate.request_id ?? null,
      ]);
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      errors.push(candidate);
    }
    return errors;
  });
  const globalError = computed(
    () =>
      actionError.value ??
      firstTurnRecoveryError.value ??
      conversationCreationUncertaintyError.value ??
      readError.value,
  );
  const lastRequestId = ref<string | null>(null);

  type ActionErrorSource =
    | "CONVERSATION_CREATE"
    | "MUTATION"
    | "RECONCILIATION";
  type ReadErrorSource =
    | "CONVERSATIONS"
    | "TIMELINE"
    | "TASK_HISTORY";
  let actionErrorSource: ActionErrorSource | null = null;
  let readErrorSource: ReadErrorSource | null = null;
  let conversationGeneration = 0;
  let timelineGeneration = 0;
  let disposed = false;
  let conversationController: AbortController | null = null;
  let timelineController: AbortController | null = null;
  const taskHistoryControllers = new Map<string, AbortController>();
  const taskHistoryGenerations = new Map<string, number>();

  const mutationOptions = {
    send: async (descriptor: PendingMutationV1) => {
      switch (descriptor.operation) {
        case "TASK_CREATE":
        case "TASK_INPUT_SUPPLEMENT":
          return api.submitMessage(
            descriptor.resourceId,
            descriptor.body,
            descriptor.idempotencyKey,
          );
        case "TOOL_RETRY":
          return api.retryTool(
            descriptor.resourceId,
            descriptor.body,
            descriptor.idempotencyKey,
          );
        case "EXPLANATION_RETRY":
          return api.retryExplanation(
            descriptor.resourceId,
            descriptor.body,
            descriptor.idempotencyKey,
          );
      }
    },
    ...(options.storage === undefined
      ? {}
      : { storage: options.storage }),
    ...(options.keyFactory === undefined
      ? {}
      : { keyFactory: options.keyFactory }),
  };
  const mutation = useIdempotentRequest(mutationOptions);
  const pendingMutation = computed<
    PendingMutationV1 | FirstTurnDescriptorV1 | null
  >(() => firstTurn.pending.value ?? mutation.pending.value);
  const mutationStatus = computed<MutationStatus>(() => {
    if (
      mutation.status.value === "SENDING" ||
      mutation.status.value === "UNCERTAIN" ||
      mutation.status.value === "BUSINESS_FAILED"
    ) {
      return mutation.status.value;
    }
    return firstTurn.status.value === "IDLE"
      ? mutation.status.value
      : firstTurn.status.value;
  });

  function setActionVisibleError(
    error: UserVisibleError,
    source: ActionErrorSource,
  ): void {
    actionError.value = error;
    actionErrorSource = source;
  }

  function setActionError(
    error: unknown,
    source: ActionErrorSource,
  ): void {
    setActionVisibleError(toUserVisibleError(error), source);
  }

  function clearActionError(): void {
    actionError.value = null;
    actionErrorSource = null;
  }

  function clearReadError(source: ReadErrorSource): void {
    if (readErrorSource !== source) {
      return;
    }
    readError.value = null;
    readErrorSource = null;
  }

  function setReadError(
    error: unknown,
    source: ReadErrorSource,
  ): void {
    readError.value = toUserVisibleError(error);
    readErrorSource = source;
  }

  async function loadConversations(reset = false): Promise<void> {
    if (disposed) {
      return;
    }
    conversationController?.abort();
    const controller = new AbortController();
    conversationController = controller;
    const generation = ++conversationGeneration;
    conversationListLoading.value = true;
    clearReadError("CONVERSATIONS");
    try {
      const cursor = reset
        ? undefined
        : conversationNextCursor.value ?? undefined;
      const response = await api.listConversations(
        20,
        cursor,
        controller.signal,
      );
      if (disposed || generation !== conversationGeneration) {
        return;
      }
      if (reset) {
        const seen = new Set<string>();
        conversations.value = response.data.items.filter((item) => {
          if (seen.has(item.conversation_id)) {
            return false;
          }
          seen.add(item.conversation_id);
          return true;
        });
      } else {
        const seen = new Set(
          conversations.value.map((item) => item.conversation_id),
        );
        conversations.value = [
          ...conversations.value,
          ...response.data.items.filter((item) => {
            if (seen.has(item.conversation_id)) {
              return false;
            }
            seen.add(item.conversation_id);
            return true;
          }),
        ];
      }
      conversationNextCursor.value = response.data.next_cursor;
      lastRequestId.value = response.request_id;
    } catch (error) {
      if (
        disposed ||
        generation !== conversationGeneration ||
        error instanceof RequestAbortedError
      ) {
        return;
      }
      setReadError(error, "CONVERSATIONS");
      throw error;
    } finally {
      if (generation === conversationGeneration) {
        conversationListLoading.value = false;
        if (conversationController === controller) {
          conversationController = null;
        }
      }
    }
  }

  async function loadMoreConversations(): Promise<void> {
    if (disposed || conversationNextCursor.value === null) {
      return;
    }
    await loadConversations(false);
  }

  async function refreshTimelineWithSignal(
    signal: AbortSignal,
  ): Promise<void> {
    if (disposed) {
      return;
    }
    const conversationId = selectedConversationId.value;
    if (conversationId === null) {
      return;
    }
    const generation = ++timelineGeneration;
    timelineLoading.value = true;
    clearReadError("TIMELINE");
    try {
      const complete = await loadCompleteTimeline(
        api,
        conversationId,
        signal,
      );
      if (
        disposed ||
        generation !== timelineGeneration ||
        selectedConversationId.value !== conversationId
      ) {
        return;
      }
      timeline.value = complete.items;
      lastRequestId.value = complete.lastRequestId;
      if (actionErrorSource === "RECONCILIATION") {
        clearActionError();
      }
    } catch (error) {
      if (
        disposed ||
        generation !== timelineGeneration ||
        error instanceof RequestAbortedError
      ) {
        return;
      }
      setReadError(error, "TIMELINE");
      throw error;
    } finally {
      if (generation === timelineGeneration) {
        timelineLoading.value = false;
      }
    }
  }

  async function refreshTimeline(): Promise<void> {
    if (disposed) {
      return;
    }
    timelineController?.abort();
    const controller = new AbortController();
    timelineController = controller;
    try {
      await refreshTimelineWithSignal(controller.signal);
    } finally {
      if (timelineController === controller) {
        timelineController = null;
      }
    }
  }

  const polling = usePolling({
    poll: refreshTimelineWithSignal,
    selectDelay() {
      const hasActiveTask = timeline.value.some(
        (item) =>
          item.item_type === "TOOL_TASK" &&
          (item.task.status === "PENDING" ||
            item.task.status === "RUNNING"),
      );
      const mutationActive =
        mutationStatus.value === "SENDING" ||
        mutationStatus.value === "UNCERTAIN";
      return hasActiveTask || mutationActive ? 2_000 : 15_000;
    },
  });

  function dispose(): void {
    disposed = true;
    polling.stop();
    conversationGeneration += 1;
    timelineGeneration += 1;
    conversationController?.abort();
    conversationController = null;
    timelineController?.abort();
    timelineController = null;
    for (const [taskId, controller] of taskHistoryControllers) {
      controller.abort();
      taskHistoryGenerations.set(
        taskId,
        (taskHistoryGenerations.get(taskId) ?? 0) + 1,
      );
    }
    taskHistoryControllers.clear();
    conversationListLoading.value = false;
    timelineLoading.value = false;
    taskDetailsLoadingById.value = {};
  }

  if (getCurrentScope() !== undefined) {
    onScopeDispose(dispose);
  }

  async function initialize(): Promise<void> {
    await loadConversations(true);
  }

  async function refreshConversations(): Promise<void> {
    await loadConversations(true);
    if (disposed) {
      return;
    }
    conversationCreationUncertain.value = false;
  }

  async function createConversation(
    title?: string,
  ): Promise<Conversation> {
    if (conversationCreationUncertain.value) {
      throw new ConversationCreationUncertaintyError();
    }
    clearActionError();
    let response: ApiSuccessEnvelope<Conversation>;
    try {
      response = await api.createConversation(title);
    } catch (error) {
      if (!disposed) {
        if (error instanceof ConversationCreationUncertaintyError) {
          conversationCreationUncertain.value = true;
        } else {
          setActionError(error, "CONVERSATION_CREATE");
        }
      }
      throw error;
    }

    if (disposed) {
      return response.data;
    }
    conversationCreationUncertain.value = false;
    lastRequestId.value = response.request_id;
    let reconciliationFailed = false;
    try {
      await loadConversations(true);
    } catch {
      reconciliationFailed = true;
    }
    if (disposed) {
      return response.data;
    }
    try {
      await selectConversation(response.data.conversation_id);
    } catch {
      reconciliationFailed = true;
    }
    if (disposed) {
      return response.data;
    }
    lastRequestId.value = response.request_id;
    if (reconciliationFailed) {
      setActionVisibleError(
        { message: REFRESH_AFTER_WRITE_MESSAGE },
        "RECONCILIATION",
      );
    }
    return response.data;
  }

  function showBlankWorkspace(): void {
    if (firstTurn.pending.value !== null) {
      return;
    }
    timelineController?.abort();
    timelineController = null;
    timelineGeneration += 1;
    selectedConversationId.value = null;
    timeline.value = [];
    supplementTarget.value = null;
  }

  async function deleteConversation(conversationId: string): Promise<void> {
    if (firstTurn.pending.value !== null || mutationStatus.value === "SENDING") {
      throw new Error("当前写操作尚未完成，请稍后再删除对话。");
    }
    if (api.deleteConversation === undefined) {
      throw new Error("当前客户端不支持删除对话。");
    }
    clearActionError();
    try {
      const response = await api.deleteConversation(conversationId);
      if (disposed) {
        return;
      }
      lastRequestId.value = response.request_id;
      conversations.value = conversations.value.filter(
        (item) => item.conversation_id !== conversationId,
      );
      if (selectedConversationId.value === conversationId) {
        timelineController?.abort();
        timelineController = null;
        timelineGeneration += 1;
        selectedConversationId.value = null;
        timeline.value = [];
        taskDetailsById.value = {};
        taskDetailsLoadingById.value = {};
        supplementTarget.value = null;
      }
    } catch (error) {
      if (!disposed) {
        setActionError(error, "MUTATION");
      }
      throw error;
    }
  }

  async function selectConversation(
    conversationId: string,
  ): Promise<void> {
    if (disposed) {
      return;
    }
    if (selectedConversationId.value === conversationId) {
      await refreshTimeline();
      return;
    }
    const restartPolling = polling.isRunning.value;
    if (restartPolling) {
      polling.stop();
    }
    timelineController?.abort();
    timelineGeneration += 1;
    selectedConversationId.value = conversationId;
    timeline.value = [];
    supplementTarget.value = null;
    try {
      await refreshTimeline();
    } finally {
      if (restartPolling && !disposed) {
        polling.start();
      }
    }
  }

  async function loadTaskHistory(
    taskId: string,
  ): Promise<TaskDetail> {
    if (disposed) {
      throw new RequestAbortedError();
    }
    taskHistoryControllers.get(taskId)?.abort();
    const controller = new AbortController();
    taskHistoryControllers.set(taskId, controller);
    const generation =
      (taskHistoryGenerations.get(taskId) ?? 0) + 1;
    taskHistoryGenerations.set(taskId, generation);
    taskDetailsLoadingById.value = {
      ...taskDetailsLoadingById.value,
      [taskId]: true,
    };
    clearReadError("TASK_HISTORY");
    try {
      const response = await api.getTask(taskId, controller.signal);
      if (
        disposed ||
        taskHistoryGenerations.get(taskId) !== generation
      ) {
        return response.data;
      }
      taskDetailsById.value = {
        ...taskDetailsById.value,
        [taskId]: response.data,
      };
      lastRequestId.value = response.request_id;
      return response.data;
    } catch (error) {
      if (
        !disposed &&
        taskHistoryGenerations.get(taskId) === generation &&
        !(error instanceof RequestAbortedError)
      ) {
        setReadError(error, "TASK_HISTORY");
      }
      throw error;
    } finally {
      if (
        !disposed &&
        taskHistoryGenerations.get(taskId) === generation
      ) {
        taskDetailsLoadingById.value = {
          ...taskDetailsLoadingById.value,
          [taskId]: false,
        };
        if (taskHistoryControllers.get(taskId) === controller) {
          taskHistoryControllers.delete(taskId);
        }
      }
    }
  }

  function setSupplementTarget(target: SupplementTarget): void {
    supplementTarget.value = { ...target };
  }

  function cancelSupplementTarget(): void {
    supplementTarget.value = null;
  }

  function requireSelectedConversation(): string {
    const conversationId = selectedConversationId.value;
    if (conversationId === null) {
      throw new Error("请先选择 Conversation。");
    }
    return conversationId;
  }

  function validateContent(contentText: string): string {
    const trimmed = contentText.trim();
    if (!trimmed) {
      throw new Error("提交内容不能为空。");
    }
    return trimmed;
  }

  function invalidateTask(taskId: string): void {
    taskHistoryControllers.get(taskId)?.abort();
    taskHistoryControllers.delete(taskId);
    taskHistoryGenerations.set(
      taskId,
      (taskHistoryGenerations.get(taskId) ?? 0) + 1,
    );
    const next = { ...taskDetailsById.value };
    delete next[taskId];
    taskDetailsById.value = next;
    taskDetailsLoadingById.value = {
      ...taskDetailsLoadingById.value,
      [taskId]: false,
    };
  }

  function applyConfirmedMutation(
    descriptor: Pick<
      PendingMutationV1,
      "operation" | "resourceId"
    >,
  ): void {
    if (descriptor.operation === "TOOL_RETRY") {
      invalidateTask(descriptor.resourceId);
    }
    if (descriptor.operation === "EXPLANATION_RETRY") {
      const item = timeline.value.find(
        (candidate) =>
          candidate.item_type === "TOOL_TASK" &&
          candidate.task.selected_result_id === descriptor.resourceId,
      );
      if (item !== undefined) {
        invalidateTask(item.task_id);
      }
    }
    if (descriptor.operation === "TASK_INPUT_SUPPLEMENT") {
      supplementTarget.value = null;
    }
  }

  async function reconcileAfterMutation(
    descriptor: Pick<
      PendingMutationV1,
      "operation" | "resourceId"
    >,
  ): Promise<void> {
    if (disposed) {
      return;
    }
    let failed = false;
    try {
      await refreshTimeline();
    } catch {
      failed = true;
    }
    if (disposed) {
      return;
    }
    if (
      descriptor.operation === "TASK_CREATE" ||
      descriptor.operation === "TASK_INPUT_SUPPLEMENT"
    ) {
      try {
        await loadConversations(true);
      } catch {
        failed = true;
      }
    }
    if (disposed) {
      return;
    }
    if (failed) {
      setActionVisibleError(
        { message: REFRESH_AFTER_WRITE_MESSAGE },
        "RECONCILIATION",
      );
    }
  }

  async function refreshAfterBusinessFailure(
    descriptor: Pick<
      PendingMutationV1,
      "operation" | "resourceId"
    >,
  ): Promise<void> {
    if (disposed) {
      return;
    }
    try {
      await refreshTimeline();
    } catch {
      // The read path owns its safe readError projection.
    }
    if (
      disposed ||
      (descriptor.operation !== "TASK_CREATE" &&
        descriptor.operation !== "TASK_INPUT_SUPPLEMENT")
    ) {
      return;
    }
    try {
      await loadConversations(true);
    } catch {
      // The read path owns its safe readError projection.
    }
  }

  async function runMutation(
    operation: MutationOperation,
    resourceId: string,
    body: MutationRequestBody,
  ): Promise<void> {
    if (firstTurn.pending.value !== null) {
      throw new Error("首条消息尚未完整确认，请继续重试原请求。");
    }
    clearActionError();
    let response: ApiSuccessEnvelope<unknown>;
    try {
      response = await mutation.start<ApiSuccessEnvelope<unknown>>(
        operation,
        resourceId,
        body,
      );
    } catch (error) {
      if (!disposed) {
        setActionError(error, "MUTATION");
      }
      if (error instanceof ApiResponseError) {
        await refreshAfterBusinessFailure({
          operation,
          resourceId,
        });
      }
      throw error;
    }
    if (disposed) {
      return;
    }
    lastRequestId.value = response.request_id;
    applyConfirmedMutation({ operation, resourceId });
    await reconcileAfterMutation({ operation, resourceId });
    lastRequestId.value = response.request_id;
  }

  async function submitNewTask(contentText: string): Promise<void> {
    if (selectedConversationId.value === null) {
      clearActionError();
      try {
        const response = await firstTurn.start(validateContent(contentText));
        if (disposed) {
          return;
        }
        lastRequestId.value = response.request_id;
        await loadConversations(true);
        if (!disposed) {
          await selectConversation(response.data.conversation_id);
          lastRequestId.value = response.request_id;
        }
      } catch (error) {
        if (!disposed) {
          setActionError(error, "MUTATION");
        }
        throw error;
      }
      return;
    }
    const conversationId = requireSelectedConversation();
    const body: NewTaskMutationBody = {
      submission_mode: "NEW_TASK",
      content_text: validateContent(contentText),
      target_task_id: null,
    };
    await runMutation(
      "TASK_CREATE",
      conversationId,
      body,
    );
  }

  async function submitSupplement(
    contentText: string,
  ): Promise<void> {
    const target = supplementTarget.value;
    if (target === null) {
      throw new Error("请先选择明确的补充目标。");
    }
    const conversationId = requireSelectedConversation();
    if (target.conversationId !== conversationId) {
      throw new Error("补充目标与当前 Conversation 不一致。");
    }
    const body: SupplementMutationBody = {
      submission_mode: "SUPPLEMENT_TASK",
      content_text: validateContent(contentText),
      target_task_id: target.taskId,
    };
    await runMutation(
      "TASK_INPUT_SUPPLEMENT",
      conversationId,
      body,
    );
  }

  async function retryTool(taskId: string): Promise<void> {
    const body: ToolRetryRequest = {
      reason: "USER_REQUESTED_RETRY",
    };
    await runMutation("TOOL_RETRY", taskId, body);
  }

  async function retryExplanation(
    resultId: string,
    language = "zh-CN",
  ): Promise<void> {
    const body: ExplanationRetryRequest = {
      language,
      reason: "USER_REQUESTED_RETRY",
    };
    await runMutation("EXPLANATION_RETRY", resultId, body);
  }

  async function retryPendingMutation(): Promise<void> {
    if (firstTurn.pending.value !== null) {
      clearActionError();
      try {
        const response = await firstTurn.retry();
        if (disposed) {
          return;
        }
        lastRequestId.value = response.request_id;
        await loadConversations(true);
        if (!disposed) {
          await selectConversation(response.data.conversation_id);
          lastRequestId.value = response.request_id;
        }
      } catch (error) {
        if (!disposed) {
          setActionError(error, "MUTATION");
        }
        throw error;
      }
      return;
    }
    const descriptor = mutation.pending.value;
    if (descriptor === null) {
      throw new Error("没有可重试的待确认写操作。");
    }
    clearActionError();
    let response: ApiSuccessEnvelope<unknown>;
    try {
      response =
        await mutation.retryPending<ApiSuccessEnvelope<unknown>>();
    } catch (error) {
      if (!disposed) {
        setActionError(error, "MUTATION");
      }
      if (error instanceof ApiResponseError) {
        await refreshAfterBusinessFailure(descriptor);
      }
      throw error;
    }
    if (disposed) {
      return;
    }
    lastRequestId.value = response.request_id;
    applyConfirmedMutation(descriptor);
    await reconcileAfterMutation(descriptor);
    lastRequestId.value = response.request_id;
  }

  function discardPendingMutation(): { discarded: boolean } {
    if (firstTurn.pending.value !== null) {
      return { discarded: false };
    }
    const result = mutation.discardPending();
    if (result.discarded) {
      clearActionError();
    }
    return result;
  }

  function startPolling(): void {
    if (!disposed) {
      polling.start();
    }
  }

  return {
    conversations: readonly(conversations),
    conversationNextCursor: readonly(conversationNextCursor),
    selectedConversationId: readonly(selectedConversationId),
    timeline: readonly(timeline),
    timelineLoading: readonly(timelineLoading),
    conversationListLoading: readonly(conversationListLoading),
    taskDetailsById: readonly(taskDetailsById),
    taskDetailsLoadingById: readonly(taskDetailsLoadingById),
    supplementTarget: readonly(supplementTarget),
    pendingMutation,
    mutationStatus,
    conversationCreationUncertain: readonly(
      conversationCreationUncertain,
    ),
    firstTurnDraft,
    globalError,
    globalErrors,
    lastRequestId: readonly(lastRequestId),
    initialize,
    loadConversations,
    refreshConversations,
    loadMoreConversations,
    createConversation,
    showBlankWorkspace,
    deleteConversation,
    selectConversation,
    refreshTimeline,
    loadTaskHistory,
    setSupplementTarget,
    cancelSupplementTarget,
    submitNewTask,
    submitSupplement,
    retryTool,
    retryExplanation,
    retryPendingMutation,
    discardPendingMutation,
    startPolling,
    stopPolling: polling.stop,
  };
}
