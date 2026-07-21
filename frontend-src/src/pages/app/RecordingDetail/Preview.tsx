import { useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { exportEmotionUrl, exportSummaryVersionUrl, exportTranscriptUrl } from "@/api/endpoints";
import { Icon } from "@/components/Icon";
import type { EmotionAnalysis, Summary, TranscriptSegment } from "@/api/types";
import { rehypeSeekTimestamps } from "@/utils/rehype-seek-timestamps";

export type ArtifactKey = string;
// "summary:<id>" | "transcript"
export function summaryArtifactKey(summary: Summary): ArtifactKey {
  return `summary:${summary.id}`;
}
export const TRANSCRIPT_KEY: ArtifactKey = "transcript";
export const EMOTION_KEY: ArtifactKey = "emotion";

// 下载菜单项样式（与组件内联，避免改动 CSS 文件）
const downloadItemStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "8px",
  width: "100%",
  padding: "6px 10px",
  background: "transparent",
  border: 0,
  borderRadius: "var(--radius-xs)",
  fontSize: "var(--text-xs)",
  color: "var(--fg-default)",
  cursor: "pointer",
  textAlign: "left",
};

interface Props {
  recordingId: string;
  summaries: Summary[];
  segments: TranscriptSegment[];
  emotion: EmotionAnalysis | null;
  /** Which artifact is being previewed. Caller hides the rail entirely when
   *  null (via .is-closed), so this component always renders something. */
  selected: ArtifactKey;
  onSelect: (key: ArtifactKey) => void;
  onClose: () => void;
  /** 点击纪要里的时间戳时调用，父组件 seek 左侧录音播放器到该秒数。 */
  onSeekToTime?: (seconds: number) => void;
}

// Build the transcript markdown from segments on the client — saves a fetch
// and keeps the rail responsive while the user is reading.
function transcriptMarkdown(segments: TranscriptSegment[]): string {
  if (segments.length === 0) return "_还没有转写。_";
  const lines: string[] = ["# 逐字稿", ""];
  for (const seg of segments) {
    const speaker = seg.speaker_name || `Speaker ${seg.speaker}`;
    lines.push(`### \`${seg.start_label}\` · ${speaker}`);
    lines.push("");
    lines.push(seg.text);
    lines.push("");
  }
  return lines.join("\n");
}

export function Preview({ recordingId, summaries, segments, emotion, selected, onSelect, onClose, onSeekToTime }: Props) {
  const currentSummary = useMemo(() => {
    if (!selected.startsWith("summary:")) return null;
    const id = selected.slice("summary:".length);
    return summaries.find((s) => s.id === id) ?? null;
  }, [selected, summaries]);
  const isTranscript = selected === TRANSCRIPT_KEY;
  const isEmotion = selected === EMOTION_KEY;

  const markdown = useMemo(() => {
    if (currentSummary) return currentSummary.content;
    if (isTranscript) return transcriptMarkdown(segments);
    if (isEmotion) return emotion?.content ?? "_还没有情绪分析。_";
    return "";
  }, [currentSummary, isTranscript, isEmotion, emotion, segments]);

  const displayName = currentSummary
    ? `会议纪要 v${currentSummary.version}`
    : isTranscript
      ? "逐字稿"
      : isEmotion
        ? "对话情绪分析"
        : "";

  // 各格式对应的下载 URL（md / docx）。复用同一个导出端点，docx 加 ?format=docx。
  const buildDownloadHref = (fmt: "md" | "docx"): string | null => {
    if (currentSummary) return exportSummaryVersionUrl(recordingId, currentSummary.id, fmt);
    if (isTranscript) return exportTranscriptUrl(recordingId, fmt);
    if (isEmotion) return exportEmotionUrl(recordingId, fmt);
    return null;
  };

  const metaText = currentSummary
    ? `${currentSummary.model}${currentSummary.is_current ? " · 当前版本" : ""}`
    : isTranscript
      ? `${segments.length} 段发言`
      : isEmotion
        ? emotion?.model ?? ""
        : "";

  function triggerDownload(fmt: "md" | "docx") {
    const href = buildDownloadHref(fmt);
    if (!href) return;
    const ext = fmt === "docx" ? "docx" : "md";
    const filename = `${displayName || "导出"}.${ext}`.replace(/[\\/:*?"<>|]/g, "_");
    // Web browser: native <a download>.
    const a = document.createElement("a");
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  const [downloadOpen, setDownloadOpen] = useState(false);

  return (
    <div
      className="preview-window"
      style={{ height: "100%", borderRadius: 0, border: 0, boxShadow: "none" }}
    >
      <header className="preview-header">
        <span className="preview-type">MD</span>
        <span className="preview-title">{displayName}</span>
        <span className="preview-meta">{metaText}</span>
        <div className="preview-actions">
          {summaries.length + (segments.length > 0 ? 1 : 0) + (emotion ? 1 : 0) > 1 && (
            <select
              className="field"
              style={{ width: 168, height: 28, fontSize: "var(--text-xs)" }}
              value={selected}
              onChange={(e) => onSelect(e.target.value)}
              aria-label="切换产物"
            >
              {summaries.map((s) => (
                <option key={s.id} value={summaryArtifactKey(s)}>
                  会议纪要 v{s.version}
                  {s.is_current ? " · 当前" : ""}
                </option>
              ))}
              {emotion && <option value={EMOTION_KEY}>对话情绪分析</option>}
              {segments.length > 0 && (
                <option value={TRANSCRIPT_KEY}>逐字稿</option>
              )}
            </select>
          )}
          {buildDownloadHref("md") && (
            <div className="download-menu" style={{ position: "relative" }}>
              <button
                type="button"
                className="icon-btn"
                aria-label="下载"
                title="下载"
                onClick={() => setDownloadOpen((v) => !v)}
              >
                <Icon name="download" size={14} />
              </button>
              {downloadOpen && (
                <>
                  {/* 点击外部关闭菜单 */}
                  <div
                    style={{ position: "fixed", inset: 0, zIndex: 10 }}
                    onClick={() => setDownloadOpen(false)}
                  />
                  <div
                    className="download-menu__panel"
                    style={{
                      position: "absolute",
                      right: 0,
                      top: "100%",
                      marginTop: 4,
                      zIndex: 11,
                      background: "var(--bg-surface)",
                      border: "1px solid var(--border-default)",
                      borderRadius: "var(--radius-sm)",
                      boxShadow: "0 8px 24px -8px oklch(0 0 0 / 0.18)",
                      padding: "4px",
                      minWidth: 120,
                    }}
                  >
                    <button
                      type="button"
                      className="download-menu__item"
                      style={downloadItemStyle}
                      onClick={() => { triggerDownload("docx"); setDownloadOpen(false); }}
                    >
                      <Icon name="file-text" size={13} /> Word (.docx)
                    </button>
                    <button
                      type="button"
                      className="download-menu__item"
                      style={downloadItemStyle}
                      onClick={() => { triggerDownload("md"); setDownloadOpen(false); }}
                    >
                      <Icon name="file" size={13} /> Markdown (.md)
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
          <button type="button" className="icon-btn" aria-label="关闭预览" onClick={onClose}>
            <Icon name="x" size={14} />
          </button>
        </div>
      </header>

      <div className="preview-body">
        <article className="preview-doc markdown">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            rehypePlugins={onSeekToTime ? [rehypeSeekTimestamps] : []}
            components={
              onSeekToTime
                ? {
                    // 拦截时间戳链接（rehype 生成的 <a class="seek-timestamp">，
                    // href 形如 #t=750），点击时 seek 播放器而非跳转。
                    // 从 href 解析秒数最可靠（不依赖 hast propertyize 规则）。
                    a({ href, children, ...rest }) {
                      if (typeof href === "string" && href.startsWith("#t=")) {
                        const seconds = Number(href.slice(3));
                        if (Number.isFinite(seconds)) {
                          return (
                            <a
                              href={href}
                              className="seek-timestamp"
                              title={`点击跳到 ${formatSeekLabel(seconds)}`}
                              onClick={(e) => {
                                e.preventDefault();
                                onSeekToTime(seconds);
                              }}
                              {...rest}
                            >
                              {children}
                            </a>
                          );
                        }
                      }
                      return <a href={href} {...rest}>{children}</a>;
                    },
                  }
                : undefined
            }
          >
            {markdown}
          </ReactMarkdown>
        </article>
      </div>
    </div>
  );
}

/** 把秒数格式化成 mm:ss 或 hh:mm:ss，用于时间戳链接的 title 提示。 */
function formatSeekLabel(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}
