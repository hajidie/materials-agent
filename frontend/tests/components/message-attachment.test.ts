import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, it, vi } from "vitest";

import MessageAttachment from "../../src/components/MessageAttachment.vue";

const response = (data: unknown) => new Response(JSON.stringify({ request_id: "request", data }), { status: 200 });

afterEach(() => vi.unstubAllGlobals());

it("renders image artifacts as a clickable thumbnail instead of a details card", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => response({
    attachment_id: "image-ref",
    kind: "image",
    name: "结果图片",
    image_url: "/api/v1/assets/image-ref/content",
    downloads: [],
  })));
  const wrapper = mount(MessageAttachment, { props: {
    conversationId: "conversation",
    messageId: "answer",
    attachment: { attachment_id: "image-ref", kind: "image", name: "结果图片" },
  } });

  await flushPromises();

  const preview = wrapper.get(".image-preview");
  expect(preview.get("img").attributes("src")).toBe("/api/v1/assets/image-ref/content");
  expect(wrapper.text()).not.toContain("查看详情");
  await preview.trigger("click");
  expect(wrapper.emitted("open")).toHaveLength(1);
});

it("keeps non-image artifacts as details cards without fetching them", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  const wrapper = mount(MessageAttachment, { props: {
    conversationId: "conversation",
    messageId: "message",
    attachment: { attachment_id: "dataset-ref", kind: "dataset", name: "实验数据" },
  } });

  await flushPromises();

  expect(wrapper.get(".attachment-card").text()).toContain("查看详情");
  expect(fetch).not.toHaveBeenCalled();
});
