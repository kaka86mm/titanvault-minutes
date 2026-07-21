// 时间戳解析：把纪要/转写里的时间戳文本解析成秒数，用于音频 seek。
//
// 后端 seconds_label() 生成 "HH:MM:SS"（如 "00:12:30"），LLM 写纪要时
// 会引用这些时间戳作为原文证据。但 LLM 输出不完全规整，可能产生：
//   - "12:30"（省略小时，短会议常见）
//   - "00:12:30"（标准 HH:MM:SS）
//   - "1:02:30"（小时不补零）
// 这里统一解析成秒数，容错上述变体。

export interface ParsedTimestamp {
  /** 秒数，用于 audio.currentTime = seconds */
  seconds: number;
  /** 在原文中匹配到的完整文本，如 "00:12:30" */
  raw: string;
}

/**
 * 解析单个时间戳字符串为秒数。
 * 支持 "HH:MM:SS" / "H:MM:SS" / "MM:SS" / "M:SS"。
 * 无效格式返回 null。
 */
export function parseTimestamp(text: string): number | null {
  const trimmed = text.trim();
  // HH:MM:SS 或 H:MM:SS
  let m = trimmed.match(/^(\d{1,2}):(\d{2}):(\d{2})$/);
  if (m) {
    const h = parseInt(m[1], 10);
    const min = parseInt(m[2], 10);
    const sec = parseInt(m[3], 10);
    if (min >= 60 || sec >= 60) return null;
    return h * 3600 + min * 60 + sec;
  }
  // MM:SS 或 M:SS
  m = trimmed.match(/^(\d{1,2}):(\d{2})$/);
  if (m) {
    const min = parseInt(m[1], 10);
    const sec = parseInt(m[2], 10);
    if (sec >= 60) return null;
    return min * 60 + sec;
  }
  return null;
}

/**
 * 在文本中找出所有时间戳，返回匹配结果（带位置）。
 * 用于把纪要文本里的时间戳替换成可点击的链接。
 *
 * 匹配策略：用正则找出所有候选，再逐个用 parseTimestamp 校验合法性
 *（过滤掉 "99:99" 这类形似但非法的）。
 */
export function findTimestamps(text: string): ParsedTimestamp[] {
  const results: ParsedTimestamp[] = [];
  // 候选正则：1-2位数字:2位数字(:2位数字)?，前后需要是非数字边界
  // 避免 "2024-01-01 12:30" 里的日期部分误匹配（日期的冒号不在此模式里，
  // 但 "12:30" 仍会被匹配——这是期望行为，它就是个时间）。
  // 用 lookbehind/lookahead 排除被更多数字包裹的情况（如版本号 "v1.2:30"）。
  const re = /(?<!\d)(\d{1,2}:\d{2}(?::\d{2})?)(?!\d)/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    const raw = match[1];
    const seconds = parseTimestamp(raw);
    if (seconds !== null) {
      results.push({ seconds, raw });
    }
  }
  return results;
}
