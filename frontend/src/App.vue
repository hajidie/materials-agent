<script setup lang="ts">
import { useMaterialsAgent } from "./composables/useMaterialsAgent";

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || "/api/v1";
const { mutationStatus } = useMaterialsAgent();
</script>

<template>
  <main
    class="app-shell"
    :data-api-base="apiBaseUrl"
    :data-mutation-status="mutationStatus"
  >
    <section class="status-card" aria-labelledby="page-title">
      <p class="eyebrow">本地材料研究平台</p>
      <h1 id="page-title">材料智能体</h1>
      <p>M10-A 前端基础已就绪</p>
      <p class="status-text">前端数据层已加载。</p>
    </section>
  </main>
</template>
