import { mount } from "@vue/test-utils";
import { expect, it } from "vitest";
import AssistantMarkdown from "../../src/components/AssistantMarkdown.vue";

it('renders research Markdown and preserves literal final punctuation', () => {
  const w = mount(AssistantMarkdown, { props: { text: '# 研究\n\n- 条件\n\n|量|值|\n|-|-|\n|强度|2|\n\n```python\nx = 1\n```\n\n末尾 | $ *' } });
  expect(w.get('h1').text()).toBe('研究');
  expect(w.get('td').text()).toBe('强度');
  expect(w.get('pre code').element.textContent).toBe('x = 1\n');
  expect(w.text()).toContain('末尾 | $ *');
});

it('repairs only the streaming display, then uses the exact final source', async () => {
  const w = mount(AssistantMarkdown, { props: { text: '结果 **重要', complete: false } });
  expect(w.get('strong').text()).toBe('重要');
  await w.setProps({ complete: true });
  expect(w.text()).toContain('结果 **重要');
  expect(w.find('strong').exists()).toBe(false);
});

it.each(['$x^2$', '$$x^2$$', '\\(x^2\\)', '\\[x^2\\]'])('defers math until completion: %s', async text => {
  const w = mount(AssistantMarkdown, { props: { text, complete: false } });
  expect(w.find('.katex').exists()).toBe(false);
  expect(w.text()).toContain('x^2');
  await w.setProps({ complete: true });
  expect(w.find('.katex').exists()).toBe(true);
});

it('does not execute HTML, unsafe URLs, remote images or trusted TeX commands', () => {
  const w = mount(AssistantMarkdown, { props: { text: '<script>alert(1)</script>\n\n[坏](javascript:alert(1)) ![图](https://example.test/tracker)\n\n$\\href{javascript:alert(1)}{click}$\n\n[来源](https://example.test/paper)' } });
  expect(w.find('script,img').exists()).toBe(false);
  const links = w.findAll('a');
  expect(links).toHaveLength(1);
  expect(links[0]!.attributes('href')).toBe('https://example.test/paper');
  expect(links[0]!.attributes('rel')).toContain('noopener');
});
