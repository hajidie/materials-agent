<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";
import remend from "remend";
import { katex } from "@mdit/plugin-katex";
import "katex/dist/katex.min.css";

const props = withDefaults(defineProps<{ text: string; complete?: boolean; compact?: boolean }>(), { complete: true, compact: false });
const md = new MarkdownIt({ html: false, linkify: false, typographer: false }).use(katex, {
  delimiters: "all", trust: false, throwOnError: false, allowInlineWithSpace: false, mathFence: false,
});
const validate = md.validateLink.bind(md);
md.validateLink = (url: string) => validate(url) && /^(https?:\/\/|\/(?!\/)|#)/i.test(url);
md.renderer.rules.image = (tokens, index) => md.utils.escapeHtml(tokens[index]?.content || "图片请查看结果附件");
const linkOpen = md.renderer.rules.link_open;
md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
  tokens[index]?.attrSet("rel", "noopener noreferrer");
  tokens[index]?.attrSet("target", "_blank");
  return linkOpen ? linkOpen(tokens, index, options, env, renderer) : renderer.renderToken(tokens, index, options);
};
for (const name of ["math_inline", "math_block"]) {
  const render = md.renderer.rules[name];
  if (render) md.renderer.rules[name] = (tokens, index, options, env, renderer) => {
    if (env?.complete) return render(tokens, index, options, env, renderer);
    const token = tokens[index]!;
    const close = token.markup === "\\(" ? "\\)" : token.markup === "\\[" ? "\\]" : token.markup;
    const tag = name === "math_block" ? "pre" : "span";
    return `<${tag} class="math-pending">${md.utils.escapeHtml(token.markup + token.content + close)}</${tag}>`;
  };
}
const html = computed(() => md.render(props.complete ? props.text : remend(props.text, {
  linkMode: "text-only", katex: false, inlineKatex: false,
}), { complete: props.complete }));
</script>

<template>
  <div class="assistant-markdown" :class="{ 'assistant-markdown--compact': compact }" v-html="html" />
</template>
