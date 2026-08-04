import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import ConversationSidebar from "../../src/components/ConversationSidebar.vue";
import type { ConversationListItem } from "../../src/api/types";

function conversation(
  id: string,
  title: string | null,
  updatedAt: string,
  preview: string | null = null,
): ConversationListItem {
  return {
    conversation_id: id,
    title,
    created_at: "2026-07-24T08:00:00Z",
    updated_at: updatedAt,
    last_activity_preview: preview,
  };
}

function mountSidebar(
  overrides: Partial<{
    conversations: ConversationListItem[];
    selectedConversationId: string | null;
    loading: boolean;
    hasMore: boolean;
    creating: boolean;
  }> = {},
) {
  return mount(ConversationSidebar, {
    props: {
      conversations: [],
      selectedConversationId: null,
      loading: false,
      hasMore: false,
      creating: false,
      ...overrides,
    },
  });
}

describe("ConversationSidebar", () => {
  it("shows the approved agent title without the old platform eyebrow", () => {
    const wrapper = mountSidebar();
    const header = wrapper.get(".conversation-sidebar__header");

    expect(header.get("h1").text()).toBe("高端金属材料组织图像智能体");
    expect(header.find(".eyebrow").exists()).toBe(false);
    expect(header.text()).not.toContain("本地材料研究平台");
  });

  it("keeps Backend order and marks only the selected Conversation", () => {
    const wrapper = mountSidebar({
      conversations: [
        conversation("older-time-first", "第一项", "2026-07-23T00:00:00Z"),
        conversation("newer-time-second", "第二项", "2026-07-24T00:00:00Z"),
      ],
      selectedConversationId: "newer-time-second",
    });

    const items = wrapper.findAll("[data-conversation-id]");
    expect(items.map((item) => item.attributes("data-conversation-id"))).toEqual(
      ["older-time-first", "newer-time-second"],
    );
    expect(items[0]?.classes()).not.toContain("conversation-list__item--selected");
    expect(items[1]?.attributes("aria-current")).toBe("true");
  });

  it("emits create once and disables the button while creating", async () => {
    const wrapper = mountSidebar();
    const create = wrapper.get("[data-action=create-conversation]");

    await create.trigger("click");
    expect(wrapper.emitted("create")).toHaveLength(1);

    await wrapper.setProps({ creating: true });
    expect(create.attributes()).toHaveProperty("disabled");
    await create.trigger("click");
    expect(wrapper.emitted("create")).toHaveLength(1);
    expect(create.text()).toContain("创建中");
  });

  it("emits an explicit list refresh and disables only that read while loading", async () => {
    const wrapper = mountSidebar();
    const refresh = wrapper.get("[data-action=refresh-conversations]");

    await refresh.trigger("click");
    expect(wrapper.emitted("refresh")).toHaveLength(1);
    expect(wrapper.emitted("create")).toBeUndefined();

    await wrapper.setProps({ loading: true });
    expect(refresh.attributes()).toHaveProperty("disabled");
    await refresh.trigger("click");
    expect(wrapper.emitted("refresh")).toHaveLength(1);
  });

  it("emits the opaque selected Conversation id", async () => {
    const wrapper = mountSidebar({
      conversations: [
        conversation("opaque:conversation/value", "材料讨论", "invalid"),
      ],
    });

    await wrapper.get("[data-conversation-id]").trigger("click");

    expect(wrapper.emitted("select")).toEqual([
      ["opaque:conversation/value"],
    ]);
  });

  it("shows load more only with a cursor and blocks duplicate loading", async () => {
    const wrapper = mountSidebar({ hasMore: true });
    const loadMore = wrapper.get("[data-action=load-more]");

    await loadMore.trigger("click");
    expect(wrapper.emitted("load-more")).toHaveLength(1);

    await wrapper.setProps({ loading: true });
    expect(loadMore.attributes()).toHaveProperty("disabled");
    await loadMore.trigger("click");
    expect(wrapper.emitted("load-more")).toHaveLength(1);

    await wrapper.setProps({ loading: false, hasMore: false });
    expect(wrapper.find("[data-action=load-more]").exists()).toBe(false);
  });

  it("uses safe title and preview fallbacks without exposing ids", () => {
    const wrapper = mountSidebar({
      conversations: [
        conversation(
          "internal-id-not-title",
          null,
          "2026-07-24T00:00:00Z",
          null,
        ),
        conversation(
          "second-id",
          "  ",
          "2026-07-24T00:00:00Z",
          "最近一条材料消息",
        ),
      ],
    });

    expect(
      wrapper
        .findAll("[data-conversation-id] strong")
        .map((entry) => entry.text()),
    ).toEqual(["新对话", "新对话"]);
    expect(wrapper.text()).toContain("最近一条材料消息");
    expect(wrapper.text()).not.toContain("internal-id-not-title");
  });

  it("shows empty and loading states safely", async () => {
    const wrapper = mountSidebar();

    expect(wrapper.text()).toContain("还没有对话");
    await wrapper.setProps({ loading: true });
    expect(wrapper.text()).toContain("正在加载对话");
  });
});
