<script setup lang="ts">
import { computed, ref, type DeepReadonly } from "vue";

import {
  resolvePublicContentUrl,
  withAttachmentDisposition,
} from "../api/client";
import type { AssetSummary } from "../api/types";

defineOptions({ name: "AssetGallery" });

const props = defineProps<{
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

function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KiB`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}
</script>

<template>
  <section v-if="displayAssets.length > 0" class="asset-gallery">
    <h3>生成图片</h3>
    <div class="asset-gallery__grid">
      <figure
        v-for="entry in displayAssets"
        :key="entry.asset.asset_id"
        class="asset-card"
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
            <p>图片加载失败。结构化结果仍可查看。</p>
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
            v-show="!failedById[entry.asset.asset_id]"
            :src="entry.inlineUrl"
            alt="生成的 SEM 图像"
            @load="markLoaded(entry.asset.asset_id)"
            @error="markFailed(entry.asset.asset_id)"
          />
          <figcaption>
            <span>
              {{ entry.asset.width }} × {{ entry.asset.height }} px ·
              {{ entry.asset.bit_depth }} bit ·
              {{ formatBytes(entry.asset.size_bytes) }}
            </span>
            <a :href="entry.attachmentUrl" download>下载图片</a>
          </figcaption>
        </template>
        <p v-else class="notice notice--error" role="alert">
          图片地址不可用。结构化结果仍可查看。
        </p>
      </figure>
    </div>
  </section>
</template>
