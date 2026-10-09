<script setup lang="ts">
import { computed } from "vue";
import type { SegmentationItem } from "../api/agent";
import type { Attachment } from "../api/artifacts";
import EbsdImage from "./EbsdImage.vue";
const props = defineProps<{ items: SegmentationItem[]; message: string }>();
defineEmits<{ artifact: [value: { message: string; attachment: Attachment }] }>();
const completed = computed(() => props.items.filter(item => ['SUCCEEDED', 'FAILED'].includes(item.status)).length);
const labels: Record<string, string> = { NOT_DISPATCHED: '等待处理', DISPATCHED: '正在分割', RESULT_RECEIVED: '正在保存', SUCCEEDED: '已完成', FAILED: '失败', OUTCOME_UNKNOWN: '待核查', SKIPPED: '停止后未执行' };
function attachment(identity: string, name: string): Attachment { return { attachment_id: identity, kind: 'image', name }; }
</script>
<template>
  <section class="segmentation-batch" aria-label="逐图分割结果">
    <p role="status">已处理 {{ completed }}/{{ items.length }} 张图片</p>
    <div class="segmentation-batch__table">
      <table><thead><tr><th>图片</th><th>状态</th><th>尺寸</th><th>预测面积占比</th></tr></thead>
        <tbody><tr v-for="item in items" :key="item.ordinal"><td>{{ item.name }}</td><td>{{ labels[item.status] ?? '待核查' }}</td>
          <td>{{ item.width && item.height ? `${item.width} × ${item.height}` : '—' }}</td><td>{{ item.area_fraction !== undefined ? `${(item.area_fraction * 100).toFixed(2)}%` : '—' }}</td></tr></tbody>
      </table>
    </div>
    <section v-for="item in items" :key="item.ordinal" class="segmentation-batch__item" :aria-label="item.name">
      <p>{{ item.name }}<span v-if="item.foreground_pixels !== undefined"> · 前景 {{ item.foreground_pixels }} / {{ item.total_pixels }} 像素</span></p>
      <p v-if="item.error" class="field-error">{{ item.error.message }}</p>
      <div class="segmentation-batch__images">
        <button v-for="role in (['overlay', 'mask'] as const)" v-show="item.artifacts[role]" :key="role" class="segmentation-batch__image" type="button"
          @click="$emit('artifact', { message, attachment: attachment(item.artifacts[role]!, `${item.name} · ${role === 'overlay' ? '叠加图' : '二值掩膜'}`) })">
          <EbsdImage v-if="item.artifacts[role]" :asset-id="item.artifacts[role]!" :image-label="`${item.name} · ${role === 'overlay' ? '叠加图' : '二值掩膜'}`" preview />
          <span>{{ role === 'overlay' ? '叠加图' : '二值掩膜' }} · 查看与下载</span>
        </button>
      </div>
    </section>
    <p class="muted">面积占比按整张输入图片计算，包含文件中的边框。没有人工标注，不能据此评价分割准确率。</p>
  </section>
</template>
