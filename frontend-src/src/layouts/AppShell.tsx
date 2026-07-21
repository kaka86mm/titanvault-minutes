import { NavLink, Outlet, useLocation } from "react-router-dom";
import { Icon, type IconName } from "@/components/Icon";
import { useAuth } from "@/context/auth";
import { cn } from "@/utils/cn";
import { Avatar } from "@/components/Avatar";

interface NavItemDef {
  to: string;
  icon: IconName;
  label: string;
  count?: number | string;
  badge?: string;
  end?: boolean;
}

interface NavSectionDef {
  title: string;
  items: NavItemDef[];
}

function memberSections(): NavSectionDef[] {
  return [
    {
      title: "AI 工作台",
      items: [
        { to: "/app/recordings/new", icon: "house", label: "首页" },
        { to: "/app/recordings", icon: "audio-lines", label: "录音库", end: true },
        { to: "/app/tasks", icon: "list-checks", label: "任务进度" },
      ],
    },
    {
      title: "知识与样本",
      items: [
        { to: "/app/hotwords", icon: "spell-check", label: "热词" },
        { to: "/app/voiceprints", icon: "mic", label: "声纹" },
      ],
    },
    {
      title: "系统",
      items: [{ to: "/app/settings", icon: "settings", label: "设置" }],
    },
  ];
}

function buildBreadcrumb(pathname: string): { href?: string; label: string }[] {
  const segments = pathname.split("/").filter(Boolean);
  if (segments.length === 0) return [];
  const labelMap: Record<string, string> = {
    app: "工作台",
    admin: "管理控制台",
    recordings: "录音库",
    new: "上传录音",
    tasks: "任务进度",
    hotwords: "热词",
    voiceprints: "声纹",
    overview: "总览",
    users: "用户",
    teams: "团队",
    permissions: "角色映射",
    wecom: "企业微信",
    hotword: "热词总览",
    "hotword-sources": "热词来源",
    audit: "审计日志",
    asr: "ASR 配置",
    llm: "大模型",
    storage: "存储",
    "storage-queue": "队列",
    export: "导出",
    "export-templates": "导出模板",
  };
  // UUID-looking segments are record IDs — replace with a friendly tail label
  // based on the preceding segment (e.g. /recordings/<uuid> → "录音详情").
  const tailFor: Record<string, string> = {
    recordings: "录音详情",
    users: "用户详情",
    teams: "团队详情",
  };
  const isUuid = (s: string) => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s);

  const crumbs: { href?: string; label: string }[] = [];
  let acc = "";
  for (let i = 0; i < segments.length; i += 1) {
    const seg = segments[i];
    acc += `/${seg}`;
    const isLast = i === segments.length - 1;
    let label = labelMap[seg] ?? seg;
    if (isUuid(seg)) {
      const parent = segments[i - 1] ?? "";
      label = tailFor[parent] ?? "详情";
    }
    if (i === 0 && segments.length === 1) {
      // Just "/app" — no crumb shown on landing.
      return [];
    }
    crumbs.push({ href: isLast ? undefined : acc, label });
  }
  return crumbs;
}

export function AppShell() {
  const { user } = useAuth();
  const location = useLocation();
  const sections = memberSections();
  const crumbs = buildBreadcrumb(location.pathname);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        跳到主内容
      </a>

      <aside className="app-shell__sidebar">
        <div className="app-shell__brand">
          <span className="app-shell__brand-glyph">A</span>
          <span>
            TitanVault Minutes <em>· 录音转写</em>
          </span>
        </div>
        <nav className="app-shell__sidebar-nav" aria-label="主导航">
          {sections.map((section) => (
            <div className="nav-section" key={section.title}>
              <div className="nav-section__title">{section.title}</div>
              {section.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={({ isActive }) =>
                    cn("nav-item", isActive && "is-active")
                  }
                >
                  <Icon name={item.icon} size={16} className="nav-item__icon" />
                  <span className="nav-item__label">{item.label}</span>
                  {item.count != null && (
                    <span className="nav-item__count">{item.count}</span>
                  )}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="app-shell__account">
          <Avatar name={user?.name} size="md" className="app-shell__account-avatar" />
          <span className="app-shell__account-name">
            {user?.name ?? "本机用户"}
            <span className="app-shell__account-role">本机 · 单用户</span>
          </span>
        </div>
      </aside>

      <header className="app-shell__topbar">
        <nav className="breadcrumb app-shell__topbar-breadcrumb" aria-label="位置">
          {crumbs.map((crumb, idx) => (
            <span key={`${crumb.label}-${idx}`}>
              {idx > 0 && <span className="breadcrumb__sep">/</span>}
              {crumb.href ? (
                <NavLink to={crumb.href}>{crumb.label}</NavLink>
              ) : (
                <span className="breadcrumb__current">{crumb.label}</span>
              )}
            </span>
          ))}
        </nav>
        <div className="app-shell__topbar-utils">
          <button type="button" aria-label="搜索">
            <Icon name="search" size={16} />
          </button>
          <button type="button" aria-label="通知">
            <Icon name="bell" size={16} />
          </button>
          <button
            type="button"
            aria-label="主题"
            onClick={() => document.documentElement.classList.toggle("dark")}
          >
            <Icon name="sun" size={16} />
          </button>
        </div>
      </header>

      <main className="app-shell__body" id="main">
        <Outlet />
      </main>
    </div>
  );
}
