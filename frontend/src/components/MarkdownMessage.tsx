import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Props = {
  content: string;
};

/**
 * Renders an assistant reply's Markdown (bold, lists, tables, code, links -
 * see remark-gfm) as real elements instead of printing the raw
 * `**`/`-`/`|` syntax as plain text. Only used for assistant messages -
 * user messages stay plain text (see AppShellPage.tsx), matching the
 * ChatGPT/Claude convention of only rendering the model's own output as
 * rich text. react-markdown never uses dangerouslySetInnerHTML, so this
 * is safe even though the content is model-generated.
 *
 * Tables matter more here than in the sibling document-assistant: the
 * system prompt tells the model to weave query results into a markdown
 * table when there is more than a row or two, so this IS the primary
 * rendering of tabular results (charts are a separate SSE event - see
 * ChartAdapter.tsx). Unlike that project, there is no ```chart fenced
 * block to intercept, because a chart never arrives inside the text.
 */
export default function MarkdownMessage({ content }: Props) {
  return (
    <div className="prose-message">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          ul: ({ children }) => (
            <ul className="mb-2 list-disc space-y-1 pl-5 last:mb-0">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-2 list-decimal space-y-1 pl-5 last:mb-0">{children}</ol>
          ),
          li: ({ children }) => <li>{children}</li>,
          h1: ({ children }) => (
            <h1 className="mb-2 mt-3 text-base font-semibold first:mt-0">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="mb-2 mt-3 text-base font-semibold first:mt-0">{children}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-1.5 mt-2 text-sm font-semibold first:mt-0">{children}</h3>
          ),
          a: ({ children, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="text-brand-600 underline hover:text-brand-700 dark:text-brand-400 dark:hover:text-brand-300"
            >
              {children}
            </a>
          ),
          code: ({ children, className }) => {
            const isBlock = /language-/.test(className ?? "");
            return isBlock ? (
              <code className={className}>{children}</code>
            ) : (
              <code className="rounded bg-slate-100 px-1 py-0.5 text-[0.85em] dark:bg-slate-800">
                {children}
              </code>
            );
          },
          pre: ({ children }) => (
            <pre className="mb-2 overflow-x-auto rounded-lg bg-slate-100 p-3 text-xs last:mb-0 dark:bg-slate-800">
              {children}
            </pre>
          ),
          blockquote: ({ children }) => (
            <blockquote className="mb-2 border-l-2 border-slate-300 pl-3 text-slate-600 last:mb-0 dark:border-slate-600 dark:text-slate-400">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-3 border-slate-200 dark:border-slate-700" />,
          table: ({ children }) => (
            <div className="mb-2 overflow-x-auto last:mb-0">
              <table className="border-collapse text-left text-xs">{children}</table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="border-b border-slate-300 dark:border-slate-600">{children}</thead>
          ),
          th: ({ children }) => <th className="px-2 py-1 font-semibold">{children}</th>,
          td: ({ children }) => (
            <td className="border-t border-slate-200 px-2 py-1 dark:border-slate-700">
              {children}
            </td>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
