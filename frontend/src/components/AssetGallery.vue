<script setup lang="ts">
import { computed, ref, type DeepReadonly } from "vue";

import {
  resolvePublicContentUrl,
  withAttachmentDisposition,
} from "../api/client";
import type { AssetSummary } from "../api/types";

defineOptions({ name: "AssetGallery" });

const props = defineProps<{
  imageLabel?: string;
  assets: readonly (
    | AssetSummary
    | DeepReadonly<AssetSummary>
  )[];
}>();

interface DisplayAsset {
  asset: AssetSummary | DeepReadonly<AssetSummary>;
  inlineUrl: string | null;
  attachmentUrl: string | null;
}

const loadingById = ref<Record<string, boolean>>({});
const failedById = ref<Record<string, boolean>>({});
const renderGenerationById = ref<Record<string, number>>({});

const displayAssets = computed<DisplayAsset[]>(() =>
  props.assets.map((entry) => {
    try {
      return {
        asset: entry,
        inlineUrl: resolvePublicContentUrl(entry.content_url),
        attachmentUrl: resolvePublicContentUrl(
          withAttachmentDisposition(entry.content_url),
        ),
      };
    } catch {
      return {
        asset: entry,
        inlineUrl: null,
        attachmentUrl: null,
      };
    }
  }),
);

function isLoading(assetId: string): boolean {
  return (
    failedById.value[assetId] !== true &&
    loadingById.value[assetId] !== false
  );
}

function markLoaded(assetId: string): void {
  loadingById.value = {
    ...loadingById.value,
    [assetId]: false,
  };
}

function markFailed(assetId: string): void {
  failedById.value = {
    ...failedById.value,
    [assetId]: true,
  };
  markLoaded(assetId);
}

function reloadImage(assetId: string): void {
  failedById.value = {
    ...failedById.value,
    [assetId]: false,
  };
  loadingById.value = {
    ...loadingById.value,
    [assetId]: true,
  };
  renderGenerationById.value = {
    ...renderGenerationById.value,
    [assetId]: (renderGenerationById.value[assetId] ?? 0) + 1,
  };
}

function imageRenderKey(assetId: string): string {
  return `${assetId}:${renderGenerationById.value[assetId] ?? 0}`;
}

</script>

<template>
  <section
    v-if="displayAssets.length > 0"
    class="asset-gallery"
    data-section="image"
  >
    <h3>组织图像</h3>
    <div class="asset-gallery__grid asset-gallery__grid--images">
      <figure
        v-for="entry in displayAssets"
        :key="entry.asset.asset_id"
        class="asset-card asset-card--image"
      >
        <template v-if="entry.inlineUrl && entry.attachmentUrl">
          <p
            v-if="isLoading(entry.asset.asset_id)"
            class="image-placeholder"
            role="status"
          >
            图片加载中…
          </p>
          <div
            v-if="failedById[entry.asset.asset_id]"
            class="notice notice--error"
            role="alert"
          >
            <p>图片加载失败。其他结果仍可查看。</p>
            <button
              type="button"
              class="button button--secondary"
              data-action="reload-image"
              @click="reloadImage(entry.asset.asset_id)"
            >
              重新加载图片
            </button>
          </div>
          <img
            :key="imageRenderKey(entry.asset.asset_id)"
            class="asset-card__image"
            v-show="!failedById[entry.asset.asset_id]"
            :src="entry.inlineUrl"
            :alt="imageLabel ?? '生成的 SEM 图像'"
            @load="markLoaded(entry.asset.asset_id)"
            @error="markFailed(entry.asset.asset_id)"
          />
          <figcaption>
            <a :href="entry.attachmentUrl" download>下载图片</a>
          </figcaption>
        </template>
        <p v-else class="notice notice--error" role="alert">
          图片地址不可用。其他结果仍可查看。
        </p>
      </figure>
    </div>
  </section>
</template>
