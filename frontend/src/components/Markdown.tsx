/**
 * Markdown 渲染组件
 * - 基于 react-markdown，支持 GFM（表格、删除线、任务列表、自动链接等）
 * - 代码块通过 rehype-highlight 做语法高亮（依赖 highlight.js 主题，见 main.tsx 引入）
 * - 通过 ``onColored`` 区分气泡底色：普通气泡用浅色正文主题，主题色气泡（用户消息）用反白主题
 */

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

/** 渲染变体：普通浅色气泡 / 主题色（蓝底白字）气泡 */
type MarkdownVariant = "surface" | "colored";

interface MarkdownProps {
  content: string;
  variant?: MarkdownVariant;
}

export default function Markdown({ content, variant = "surface" }: MarkdownProps) {
  const proseClass =
    variant === "colored" ? "prose prose-sm prose-on-colored max-w-none break-words" : "prose prose-sm prose-agent max-w-none break-words";

  return (
    <div className={proseClass}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // 外链统一新窗口打开，避免跳出应用
          a: ({ node, ...props }) => <a target="_blank" rel="noopener noreferrer" {...props} />,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
