import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";

import AssetGallery from "../../src/components/AssetGallery.vue";
import type { AssetSummary } from "../../src/api/types";

function asset(contentUrl = "/api/v1/assets/asset-1/content"): AssetSummary {
  return {
    asset_id: "asset-1",
    status: "AVAILABLE",
    role: "sem_image",
    media_type: "image/png",
    width: 512,
    height: 512,
    bit_depth: 8,
    size_bytes: 262144,
    sha256: "a".repeat(64),
    content_url: contentUrl,
  };
}

describe("AssetGallery", () => {
  it("renders public inline and download URLs without technical metadata", () => {
    const wrapper = mount(AssetGallery, {
      props: { assets: [asset()] },
    });

    expect(wrapper.get("section").attributes("data-section")).toBe("image");
    expect(wrapper.get("h3").text()).toBe("组织图像");
    expect(wrapper.get("img").attributes()).toMatchObject({
      src: "/api/v1/assets/asset-1/content",
      alt: "生成的 SEM 图像",
    });
    expect(wrapper.get("a[download]").attributes("href")).toBe(
      "/api/v1/assets/asset-1/content?disposition=attachment",
    );
    expect(wrapper.get("figcaption").text()).toBe("下载图片");
    expect(wrapper.text()).not.toMatch(/512|8 bit|256.0 KiB/);
  });

  it("remounts the same public image URL after an error and reload", async () => {
    const wrapper = mount(AssetGallery, {
      props: { assets: [asset()] },
    });
    const firstImage = wrapper.get("img").element;

    await wrapper.get("img").trigger("error");
    expect(wrapper.text()).toContain("图片加载失败。其他结果仍可查看。");

    await wrapper.get("[data-action=reload-image]").trigger("click");
    expect(wrapper.get("img").element).not.toBe(firstImage);
    expect(wrapper.get("img").attributes("src")).toBe(
      "/api/v1/assets/asset-1/content",
    );
  });

  it("safely hides image controls when the public URL is invalid", () => {
    const wrapper = mount(AssetGallery, {
      props: { assets: [asset("https://storage.invalid/private/image.png")] },
    });

    expect(wrapper.find("img").exists()).toBe(false);
    expect(wrapper.find("a[download]").exists()).toBe(false);
    expect(wrapper.text()).toContain("图片地址不可用。其他结果仍可查看。");
  });

  it("renders each image in a centered, width-limited image card", () => {
    const wrapper = mount(AssetGallery, {
      props: { assets: [asset()] },
    });

    expect(wrapper.get(".asset-gallery__grid").classes()).toContain(
      "asset-gallery__grid--images",
    );
    expect(wrapper.get("figure").classes()).toEqual(
      expect.arrayContaining(["asset-card", "asset-card--image"]),
    );
    expect(wrapper.get("img").classes()).toContain("asset-card__image");
  });
});
