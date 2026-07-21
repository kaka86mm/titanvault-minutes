// rehype 插件：把 markdown 文本里的时间戳（如 00:12:30）替换成可点击的
// <a> 元素，点击后 seek 音频到对应时间。
//
// 用 hast 树遍历而非字符串预处理，可以：
//   1. 自动跳过代码块（<code>/<pre> 里的内容不是 text 节点的直接子节点
//      会被 visit 跳过——实际上 code 里是 text 节点，但我们排除 code 祖先）
//   2. 不破坏已有的 markdown 链接和格式
//
// 生成的 <a> 带 data-seek-seconds 属性，Preview 组件用 ReactMarkdown 的
// components.a 拦截点击，调用 audioRef.currentTime = seconds 播放。
import type { Plugin } from "unified";
import type { Root, RootContent, Text } from "hast";
import { visit, SKIP } from "unist-util-visit";
import { parseTimestamp } from "./timestamp";

const TIMESTAMP_RE = /(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)/g;

/** hast 元素构造辅助：生成一个 <a data-seek-seconds="N">timestamp</a> */
function seekAnchor(seconds: number, raw: string): RootContent {
  return {
    type: "element",
    tagName: "a",
    properties: {
      href: `#t=${seconds}`,
      className: ["seek-timestamp"],
      // 用 data-* 携带秒数，components.a 里读取它来 seek。
      dataSeekSeconds: String(seconds),
    },
    children: [{ type: "text", value: raw } as Text],
  };
}

/** 把一个 text 节点拆成 [text | anchor | text | anchor | ...] 序列 */
function splitTextNode(textValue: string): RootContent[] {
  const parts: RootContent[] = [];
  let lastIndex = 0;
  TIMESTAMP_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = TIMESTAMP_RE.exec(textValue)) !== null) {
    const raw = match[1];
    const seconds = parseTimestamp(raw);
    if (seconds === null) continue;
    const matchStart = match.index;
    if (matchStart > lastIndex) {
      parts.push({ type: "text", value: textValue.slice(lastIndex, matchStart) } as Text);
    }
    parts.push(seekAnchor(seconds, raw));
    lastIndex = matchStart + raw.length;
  }
  if (lastIndex === 0) return []; // 没匹配到任何时间戳
  if (lastIndex < textValue.length) {
    parts.push({ type: "text", value: textValue.slice(lastIndex) } as Text);
  }
  return parts;
}

export const rehypeSeekTimestamps: Plugin<[], Root> = () => {
  return (tree) => {
    visit(tree, "text", (node: Text, index, parent) => {
      if (!parent || index === null || index === undefined) return;
      // 跳过 <code> 和 <pre> 里的文本，不把代码里的数字当时间戳
      const parentTag = (parent as { tagName?: string }).tagName;
      if (parentTag === "code" || parentTag === "pre") return;
      const parts = splitTextNode(node.value);
      if (parts.length === 0) return;
      // 用拆分后的节点序列替换原 text 节点
      (parent.children as RootContent[]).splice(index, 1, ...parts);
      // 跳过新插入的节点（已处理，避免重复），继续后面的兄弟节点。
      return [SKIP, index + parts.length];
    });
  };
};
