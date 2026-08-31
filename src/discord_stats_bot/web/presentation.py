"""Shared server-rendered presentation foundation for Kanami Web Admin."""

from html import escape

from discord_stats_bot.web.authorization import WebAdminRole

ADMIN_STYLES = r"""
:root {
  color-scheme: dark;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
    "Segoe UI", sans-serif;
  --bg-root: #090a0f;
  --bg-sidebar: #0d0e16;
  --bg-surface: #141620;
  --bg-surface-raised: #1a1c29;
  --bg-input: #10121a;
  --border-subtle: #282b3a;
  --border-strong: #41465d;
  --text-primary: #f4f1ff;
  --text-secondary: #cbc7d6;
  --text-muted: #9c98aa;
  --accent: #8b5cf6;
  --accent-hover: #a78bfa;
  --accent-soft: rgba(139, 92, 246, 0.14);
  --accent-pink: #e879f9;
  --success: #43d19e;
  --warning: #f5bd58;
  --danger: #f16f7a;
  --info: #59c7e8;
  --radius-sm: 7px;
  --radius-md: 11px;
  --radius-lg: 16px;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.28);
  --shadow-card: 0 14px 34px rgba(0, 0, 0, 0.18);
  --shadow-neon-soft: 0 0 30px rgba(139, 92, 246, 0.12);
}

* { box-sizing: border-box; }
html { min-width: 320px; background: var(--bg-root); }
body {
  min-height: 100vh;
  margin: 0;
  background:
    radial-gradient(circle at 76% -8%, rgba(139, 92, 246, 0.11), transparent 30rem),
    var(--bg-root);
  color: var(--text-primary);
  font-size: 15px;
  line-height: 1.55;
}
a { color: #b9a7ff; text-underline-offset: 3px; }
a:hover { color: #d5caff; }
button, input, select, textarea { font: inherit; }
button, a, input, select, textarea { transition: border-color 140ms ease, background-color 140ms ease, color 140ms ease, box-shadow 140ms ease; }
:focus-visible { outline: 2px solid var(--accent-hover); outline-offset: 3px; }
::selection { background: rgba(139, 92, 246, 0.38); }

.app-shell { min-height: 100vh; display: grid; grid-template-columns: 252px minmax(0, 1fr); }
.sidebar {
  position: sticky;
  top: 0;
  z-index: 10;
  height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 24px;
  overflow-y: auto;
  padding: 22px 16px 16px;
  border-right: 1px solid var(--border-subtle);
  background:
    linear-gradient(150deg, rgba(139, 92, 246, 0.09), transparent 36%),
    var(--bg-sidebar);
}
.desktop-navigation-shell { display: contents; }
.mobile-menu { display: none; }
.brand {
  display: flex;
  align-items: center;
  gap: 11px;
  padding: 0 8px;
  color: var(--text-primary);
  text-decoration: none;
}
.brand-mark {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border: 1px solid rgba(167, 139, 250, 0.55);
  border-radius: 10px;
  background: linear-gradient(145deg, rgba(139, 92, 246, 0.28), rgba(232, 121, 249, 0.08));
  box-shadow: var(--shadow-neon-soft);
  color: #efeaff;
  font-weight: 800;
  letter-spacing: -0.04em;
}
.brand-copy { display: grid; line-height: 1.15; }
.brand-copy strong { font-size: 1rem; letter-spacing: 0.02em; }
.brand-copy small { margin-top: 3px; color: var(--text-muted); font-size: 0.7rem; letter-spacing: 0.12em; text-transform: uppercase; }
.navigation { display: grid; gap: 20px; }
.nav-group { display: grid; gap: 4px; }
.nav-label {
  padding: 0 10px 5px;
  color: #6f6c7e;
  font-size: 0.68rem;
  font-weight: 750;
  letter-spacing: 0.13em;
  text-transform: uppercase;
}
.nav-link {
  position: relative;
  display: flex;
  align-items: center;
  min-height: 38px;
  padding: 8px 10px 8px 13px;
  border: 1px solid transparent;
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  font-size: 0.9rem;
  font-weight: 560;
  text-decoration: none;
}
.nav-link:hover { border-color: var(--border-subtle); background: rgba(255, 255, 255, 0.025); color: var(--text-primary); }
.nav-link[aria-current="page"] {
  border-color: rgba(139, 92, 246, 0.28);
  background: var(--accent-soft);
  color: #eee9ff;
}
.nav-link[aria-current="page"]::before {
  content: "";
  position: absolute;
  left: 5px;
  width: 2px;
  height: 17px;
  border-radius: 3px;
  background: linear-gradient(var(--accent-hover), var(--accent-pink));
  box-shadow: 0 0 9px rgba(167, 139, 250, 0.65);
}
.sidebar-footer { margin-top: auto; padding: 13px 10px 4px; border-top: 1px solid var(--border-subtle); }
.session-meta { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 9px; color: var(--text-muted); font-size: 0.76rem; }
.role-badge { color: #cabdff; font-weight: 750; letter-spacing: 0.05em; }
.logout { margin: 0; }
.logout button, .link, .link-button {
  width: 100%;
  padding: 7px 0;
  border: 0;
  background: none;
  color: var(--text-secondary);
  cursor: pointer;
  text-align: left;
}
.logout button:hover, .link:hover, .link-button:hover { color: var(--text-primary); }

.workspace { min-width: 0; }
.content {
  --content-gutter: clamp(20px, 4vw, 54px);
  width: min(100%, calc(1200px + var(--content-gutter) + var(--content-gutter)));
  margin: 0 auto;
  padding: 34px var(--content-gutter) 64px;
}
.content.wide {
  width: min(100%, calc(1450px + var(--content-gutter) + var(--content-gutter)));
}
.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 24px;
  margin-bottom: 24px;
  padding-bottom: 19px;
  border-bottom: 1px solid var(--border-subtle);
}
.page-heading { min-width: 0; }
.page-kicker { margin: 0 0 4px; color: var(--accent-hover); font-size: 0.7rem; font-weight: 750; letter-spacing: 0.14em; text-transform: uppercase; }
h1, h2, h3 { line-height: 1.2; letter-spacing: -0.02em; }
h1 { margin: 0; font-size: clamp(1.65rem, 2.6vw, 2.2rem); font-weight: 720; }
h2 { margin: 30px 0 13px; font-size: 1.08rem; font-weight: 690; }
h3 { margin: 0 0 11px; font-size: 0.95rem; }
.page-description { max-width: 760px; margin: 7px 0 0; color: var(--text-muted); }
.page-actions, .actions { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.back-link { display: inline-flex; margin-bottom: 17px; color: var(--text-secondary); font-size: 0.88rem; text-decoration: none; }
.back-link:hover { color: var(--text-primary); }
.muted, .protected, small, .help-text { color: var(--text-muted); }
.technical, code, pre { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
code { color: #d9d2ed; font-size: 0.88em; overflow-wrap: anywhere; }
pre {
  max-width: 100%;
  margin: 14px 0 0;
  padding: 15px;
  overflow: auto;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-secondary);
  font-size: 0.85rem;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.card, .panel, .status-card, .quick-card, .notice, .overall, .integrity, .history-section, .empty, .status {
  min-width: 0;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: linear-gradient(145deg, rgba(255, 255, 255, 0.012), transparent), var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.card, .panel, .integrity, .history-section { padding: 18px; }
.card { margin: 14px 0; }
.card > :first-child, .panel > :first-child { margin-top: 0; }
.card > :last-child, .panel > :last-child { margin-bottom: 0; }
.card-accent { border-color: rgba(139, 92, 246, 0.4); box-shadow: var(--shadow-neon-soft); }
.notice, .status, .empty { margin: 14px 0; padding: 13px 15px; }
.notice { border-left-width: 3px; }
.notice.success, .success { border-left-color: var(--success); }
.notice.failure, .failure, .unhealthy { border-left-color: var(--danger); }
.notice.warning { border-left-color: var(--warning); }
.notice.info { border-left-color: var(--info); }
.empty { color: var(--text-muted); text-align: center; }

.metric-grid, .status-grid, .quick-grid, .grid, .cards, .profile-grid, .availability-grid {
  display: grid;
  gap: 13px;
}
.metric-grid, .status-grid, .quick-grid, .grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 180px), 1fr)); }
.cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.metric, .status-card, .quick-card {
  min-width: 0;
  padding: 15px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
}
.metric { display: flex; flex-direction: column-reverse; gap: 5px; }
.metric strong { color: var(--text-primary); font-size: 1.25rem; font-weight: 690; overflow-wrap: anywhere; }
.metric span { color: var(--text-muted); font-size: 0.78rem; }
.metric-grid > .metric strong { font-size: 1.55rem; }
.status-card, .quick-card { display: flex; flex-direction: column; gap: 5px; }
.status-card { border-left-width: 3px; border-left-color: var(--border-strong); }
.status-card.ok { border-left-color: var(--success); }
.quick-card { color: var(--text-primary); text-decoration: none; }
.quick-card span { color: var(--text-muted); font-size: 0.85rem; }
.quick-card:hover { border-color: rgba(139, 92, 246, 0.58); background: var(--bg-surface-raised); transform: translateY(-1px); }

.badge {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  min-height: 24px;
  padding: 3px 8px;
  border: 1px solid var(--border-strong);
  border-radius: 50rem;
  background: rgba(137, 134, 154, 0.1);
  color: var(--text-secondary);
  font-size: 0.71rem;
  font-weight: 730;
  letter-spacing: 0.035em;
  line-height: 1;
  white-space: nowrap;
}
.badge.success, .badge.healthy, .badge.active, .badge.published { border-color: rgba(67, 209, 158, 0.38); background: rgba(67, 209, 158, 0.1); color: #78e1bb; }
.badge.warning, .badge.degraded, .badge.draft { border-color: rgba(245, 189, 88, 0.4); background: rgba(245, 189, 88, 0.1); color: #f8cb79; }
.badge.danger, .badge.unavailable, .badge.departed { border-color: rgba(241, 111, 122, 0.42); background: rgba(241, 111, 122, 0.1); color: #ff9ba4; }
.badge.info { border-color: rgba(89, 199, 232, 0.38); background: rgba(89, 199, 232, 0.1); color: #8bdbf1; }
.badge.accent, .badge.owner, .badge.admin { border-color: rgba(167, 139, 250, 0.42); background: var(--accent-soft); color: #cbbcff; }
.badge.neutral, .badge.archived { color: var(--text-muted); }

.table-wrap { width: 100%; overflow-x: auto; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-surface); }
.card > .table-wrap { margin-top: 12px; }
.responsive-mobile-only { display: none; }
.mobile-record-list { margin: 0; padding: 0; list-style: none; }
.mobile-record {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.mobile-record + .mobile-record { margin-top: 10px; }
.record-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 11px; }
.record-heading { min-width: 0; }
.record-title { margin: 0; font-size: 0.98rem; overflow-wrap: anywhere; }
.record-identifier { display: block; margin-top: 3px; color: var(--text-muted); font-size: 0.75rem; overflow-wrap: anywhere; }
.record-action { flex: 0 0 auto; font-size: 0.8rem; }
.record-fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; }
.record-fields div { min-width: 0; }
.record-fields dt { font-size: 0.7rem; }
.record-fields dd { margin-top: 1px; color: var(--text-secondary); font-size: 0.82rem; overflow-wrap: anywhere; }
.record-footer { margin-top: 12px; padding-top: 11px; border-top: 1px solid var(--border-subtle); }
.record-footer form { margin: 0; }
.audit-record { border-left: 3px solid rgba(139, 92, 246, 0.45); }
.audit-record time { color: var(--text-muted); font-size: 0.76rem; }

.member-directory-summary { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 13px; }
.member-directory-summary strong { font-size: 1.08rem; }
.member-directory-summary span { color: var(--text-muted); font-size: 0.82rem; }
.member-toolbar {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(150px, 0.48fr) minmax(150px, 0.48fr) auto;
  align-items: end;
  gap: 10px;
  margin-bottom: 18px;
  padding: 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.member-toolbar label { min-width: 0; display: grid; gap: 5px; }
.member-toolbar label > span { color: var(--text-muted); font-size: 0.72rem; font-weight: 680; }
.member-toolbar-actions { display: flex; align-items: center; gap: 7px; }
.member-search-reset { min-height: 38px; }
.member-directory-list { display: grid; gap: 9px; }
.member-row {
  min-width: 0;
  display: grid;
  grid-template-columns: 50px minmax(170px, 1.15fr) minmax(390px, 2fr) auto;
  align-items: center;
  gap: 13px;
  padding: 13px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  background: linear-gradient(120deg, rgba(139, 92, 246, 0.055), transparent 34%), var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.member-row:hover { border-color: rgba(139, 92, 246, 0.42); background-color: var(--bg-surface-raised); }
.member-monogram, .profile-monogram {
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border: 1px solid rgba(167, 139, 250, 0.42);
  border-radius: 50%;
  background: linear-gradient(145deg, rgba(232, 121, 249, 0.18), var(--accent-soft));
  color: #e6ddff;
  font-weight: 780;
  letter-spacing: 0.03em;
  box-shadow: inset 0 0 18px rgba(139, 92, 246, 0.1);
}
.member-avatar, .profile-avatar { position: relative; min-width: 0; overflow: hidden; border-radius: 50%; }
.member-avatar { width: 46px; height: 46px; }
.profile-avatar { width: 96px; height: 96px; }
.member-avatar .member-monogram, .profile-avatar .profile-monogram { width: 100%; height: 100%; }
.member-avatar-image { position: absolute; inset: 0; width: 100%; height: 100%; max-width: 100%; border-radius: inherit; object-fit: cover; }
.member-monogram { width: 46px; height: 46px; font-size: 0.9rem; }
.member-primary { min-width: 0; display: grid; align-content: center; }
.member-primary h2 { margin: 0; font-size: 0.96rem; line-height: 1.3; overflow-wrap: anywhere; }
.member-primary h2 a { color: var(--text-primary); text-decoration: none; }
.member-primary h2 a:hover { color: #d5caff; }
.member-username { margin-top: 2px; color: var(--text-secondary); font-size: 0.8rem; overflow-wrap: anywhere; }
.member-id { margin-top: 3px; color: var(--text-muted); font-size: 0.68rem; overflow-wrap: anywhere; }
.member-stats { display: grid; grid-template-columns: 1.35fr repeat(3, minmax(76px, 0.7fr)); gap: 8px 13px; }
.member-stats div { min-width: 0; }
.member-stats dt { font-size: 0.66rem; }
.member-stats dd { margin-top: 2px; color: var(--text-secondary); font-size: 0.78rem; overflow-wrap: anywhere; }
.member-profile-action { min-height: 38px; }
.member-directory-empty { display: grid; gap: 3px; padding-block: 24px; }
.member-directory-empty strong { color: var(--text-secondary); }
.member-directory-empty span { font-size: 0.82rem; }
.member-pagination { align-items: center; justify-content: center; }
.member-pagination .button { min-width: 104px; }
.member-pagination .disabled { pointer-events: none; opacity: 0.45; }
.pagination-position { min-width: 64px; color: var(--text-secondary); text-align: center; }

.profile-hero {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr) auto;
  align-items: center;
  gap: 18px;
  padding: 20px;
  border: 1px solid rgba(139, 92, 246, 0.35);
  border-radius: var(--radius-lg);
  background: linear-gradient(125deg, rgba(139, 92, 246, 0.14), transparent 52%), var(--bg-surface);
  box-shadow: var(--shadow-card), var(--shadow-neon-soft);
}
.profile-monogram { width: 96px; height: 96px; font-size: 1.55rem; }
.profile-primary { min-width: 0; display: grid; }
.profile-primary h2 { margin: 0; font-size: clamp(1.45rem, 3vw, 2.15rem); line-height: 1.2; overflow-wrap: anywhere; }
.profile-primary code { margin-top: 7px; color: var(--text-muted); font-size: 0.76rem; }
.profile-status { align-self: start; }
.member-profile-section { margin-top: 25px; }
.profile-identity, .profile-membership {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.profile-identity div, .profile-membership div {
  min-width: 0;
  padding: 13px 14px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  background: var(--bg-surface);
}
.profile-identity dt, .profile-membership dt { display: block; font-size: 0.7rem; }
.profile-identity dd, .profile-membership dd { display: block; margin-top: 5px; color: var(--text-secondary); overflow-wrap: anywhere; }
.profile-stat-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; }
.profile-stat-grid article { min-width: 0; padding: 16px; border: 1px solid var(--border-subtle); border-top: 2px solid rgba(139, 92, 246, 0.52); border-radius: var(--radius-md); background: var(--bg-surface); }
.profile-stat-grid span { display: block; color: var(--text-muted); font-size: 0.74rem; }
.profile-stat-grid strong { display: block; margin-top: 4px; font-size: clamp(1.25rem, 2.5vw, 1.8rem); overflow-wrap: anywhere; }
.member-activity-header { display: flex; align-items: end; justify-content: space-between; gap: 14px; }
.member-activity-header .section-heading { margin-bottom: 0; }
.member-activity-kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; margin-top: 14px; }
.member-activity-history-heading { margin-top: 20px; }
.member-activity-history { min-width: 0; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-surface); }
.member-activity-table { table-layout: fixed; }
.member-activity-table th, .member-activity-table td { width: 33.333%; overflow-wrap: anywhere; }
.member-activity-table time { color: var(--text-secondary); }
.member-activity-unavailable { margin-top: 14px; }
.member-games-header { display: flex; align-items: end; justify-content: space-between; gap: 14px; }
.member-games-header .section-heading { margin-bottom: 0; }
.member-games-kpis { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; margin-top: 14px; }
.member-games-details { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 11px; margin-top: 14px; }
.member-games-details > article { min-width: 0; padding: 16px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-surface); }
.member-games-details h3 { margin: 0 0 10px; font-size: 0.86rem; }
.member-games-details strong, .member-games-details p { overflow-wrap: anywhere; }
.member-games-details p { margin: 6px 0 0; color: var(--text-muted); font-size: 0.76rem; }
.member-games-top { display: grid; gap: 8px; margin: 0; padding-left: 22px; }
.member-games-top li { padding-left: 4px; }
.member-games-top li::marker { color: var(--text-muted); }
.member-games-top span { overflow-wrap: anywhere; }
.member-games-top strong { float: right; margin-left: 12px; color: var(--text-secondary); }
.member-games-empty, .member-games-unavailable { margin-top: 14px; }
.achievement-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 0; padding: 0; list-style: none; }
.achievement-card { min-width: 0; display: grid; grid-template-columns: 12px minmax(0, 1fr); gap: 12px; padding: 15px; border: 1px solid var(--border-subtle); border-radius: var(--radius-md); background: var(--bg-surface); }
.achievement-mark { width: 10px; height: 10px; margin-top: 5px; border: 2px solid var(--accent-hover); border-radius: 3px; transform: rotate(45deg); box-shadow: 0 0 12px rgba(139, 92, 246, 0.35); }
.achievement-copy { min-width: 0; }
.achievement-copy h3 { margin: 0 0 7px; overflow-wrap: anywhere; }
.achievement-copy p { margin: 8px 0 0; color: var(--text-secondary); font-size: 0.78rem; }
.achievement-key { display: block; margin-top: 5px; color: var(--text-muted); font-size: 0.68rem; }
.lifecycle-timeline { position: relative; margin: 0; padding: 0; list-style: none; }
.lifecycle-timeline::before { content: ""; position: absolute; top: 8px; bottom: 8px; left: 7px; width: 1px; background: var(--border-strong); }
.lifecycle-event { position: relative; display: grid; grid-template-columns: 15px minmax(0, 1fr); gap: 13px; padding: 0 0 17px; }
.lifecycle-event:last-child { padding-bottom: 0; }
.timeline-marker { z-index: 1; width: 15px; height: 15px; margin-top: 5px; border: 3px solid var(--bg-root); border-radius: 50%; background: var(--accent-hover); box-shadow: 0 0 0 1px rgba(167, 139, 250, 0.55); }
.lifecycle-event-copy { min-width: 0; padding: 12px 14px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-surface); }
.lifecycle-event-copy time { display: block; color: var(--text-muted); font-size: 0.7rem; }
.lifecycle-event-copy strong { display: block; margin-top: 3px; overflow-wrap: anywhere; }
.lifecycle-details { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 7px; color: var(--text-secondary); font-size: 0.78rem; }
.lifecycle-type { display: block; margin-top: 6px; color: var(--text-muted); font-size: 0.66rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
th, td { padding: 10px 12px; border-bottom: 1px solid var(--border-subtle); text-align: left; vertical-align: top; }
th { background: rgba(255, 255, 255, 0.018); color: var(--text-muted); font-size: 0.68rem; font-weight: 760; letter-spacing: 0.065em; text-transform: uppercase; white-space: nowrap; }
tbody tr:last-child td { border-bottom: 0; }
tbody tr:hover { background: rgba(139, 92, 246, 0.045); }
td form { margin: 0; }

form { min-width: 0; }
label { display: grid; gap: 6px; margin: 12px 0; color: var(--text-secondary); font-size: 0.84rem; font-weight: 620; }
input:not([type="checkbox"]):not([type="radio"]), select, textarea {
  width: 100%;
  min-height: 40px;
  padding: 8px 10px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg-input);
  color: var(--text-primary);
}
textarea { min-height: 150px; resize: vertical; }
input::placeholder, textarea::placeholder { color: #666476; }
input:hover, select:hover, textarea:hover { border-color: #565d79; }
input:focus, select:focus, textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.13); outline: 0; }
input[type="checkbox"], input[type="radio"] { width: 1rem; height: 1rem; margin: 0 7px 0 0; accent-color: var(--accent); }
input[type="file"] { padding: 7px; }
.checkbox-label { display: flex; align-items: center; width: fit-content; }
button, .button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 38px;
  padding: 8px 13px;
  border: 1px solid rgba(167, 139, 250, 0.35);
  border-radius: var(--radius-sm);
  background: var(--accent);
  color: white;
  font-weight: 650;
  line-height: 1.2;
  text-decoration: none;
  cursor: pointer;
}
button:hover, .button:hover { border-color: rgba(213, 202, 255, 0.55); background: var(--accent-hover); color: white; box-shadow: 0 0 18px rgba(139, 92, 246, 0.15); }
button.secondary, .button.secondary { border-color: var(--border-strong); background: var(--bg-surface-raised); color: var(--text-primary); }
button.ghost, .button.ghost { border-color: transparent; background: transparent; color: var(--text-secondary); }
button.danger, .button.danger { border-color: rgba(241, 111, 122, 0.34); background: #a83f4b; color: white; }
button.danger:hover, .button.danger:hover { background: #c44b58; }
button:disabled, input:disabled, select:disabled, textarea:disabled { cursor: not-allowed; opacity: 0.48; }
.form { display: grid; gap: 11px; }
.actions form { display: flex; align-items: end; gap: 8px; flex-wrap: wrap; }
.search { display: flex; align-items: center; gap: 8px; max-width: 650px; margin: 16px 0; }
.search input { min-width: 0; }
.pagination { display: flex; gap: 8px; margin-top: 16px; }
.pagination a { padding: 7px 11px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); text-decoration: none; }

.profile-grid { grid-template-columns: 150px minmax(0, 1fr); align-items: start; }
.avatar { width: 132px; height: 132px; object-fit: cover; border: 1px solid var(--border-strong); border-radius: 50%; background: var(--bg-surface); box-shadow: var(--shadow-neon-soft); }
.avatar.placeholder { display: grid; place-items: center; color: var(--text-muted); font-size: 0.78rem; }
dl { margin: 0; display: grid; grid-template-columns: minmax(130px, 0.6fr) minmax(0, 1fr); gap: 8px 16px; }
dt { color: var(--text-muted); }
dd { min-width: 0; margin: 0; overflow-wrap: anywhere; }
.reset-form { margin: 0 0 24px; }

.overall { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin: 0 0 16px; padding: 15px 17px; border-left-width: 3px; }
.healthy { border-left-color: var(--success); }
.degraded { border-left-color: var(--warning); }
.unavailable { border-left-color: var(--danger); }
.neutral { border-left-color: var(--border-strong); }
.card > header, .integrity > header { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 14px; }
.card > header h2, .integrity > header h2 { margin: 0; }
.metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.reasons { margin: 13px 0 0; padding-left: 18px; color: var(--text-secondary); }
.integrity, .history-section { margin: 14px 0; }
.availability-grid { grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr)); }
.availability-window { padding: 15px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-surface-raised); }
.incident-list, .integrity-list { margin: 0; padding: 0; list-style: none; }
.incident { display: grid; gap: 3px; padding: 11px 13px; border-left: 3px solid var(--border-strong); background: var(--bg-surface-raised); }
.incident + .incident, .integrity-list li + li { margin-top: 8px; }
.incident span, .incident small { color: var(--text-secondary); }
.integrity-list .healthy strong { color: var(--success); }
.integrity-list .degraded strong { color: var(--warning); }
.integrity-list .unavailable strong { color: var(--danger); }

.dashboard-hero {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(0, 2fr);
  align-items: center;
  gap: 18px 28px;
  margin-bottom: 20px;
  padding: 20px;
  border: 1px solid rgba(139, 92, 246, 0.32);
  border-radius: var(--radius-lg);
  background:
    linear-gradient(120deg, rgba(139, 92, 246, 0.12), transparent 52%),
    var(--bg-surface);
  box-shadow: var(--shadow-card), var(--shadow-neon-soft);
}
.dashboard-hero h2 { margin: 3px 0 5px; font-size: clamp(1.25rem, 2vw, 1.65rem); overflow-wrap: anywhere; }
.section-kicker { margin: 0; color: var(--text-muted); font-size: 0.68rem; font-weight: 750; letter-spacing: 0.12em; text-transform: uppercase; }
.hero-statuses { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px; }
.health-signal {
  min-width: 0;
  padding: 11px 12px;
  border: 1px solid var(--border-subtle);
  border-left: 3px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: rgba(9, 10, 15, 0.34);
}
.health-signal.success { border-left-color: var(--success); }
.health-signal.warning { border-left-color: var(--warning); }
.health-signal.danger { border-left-color: var(--danger); }
.health-signal.neutral { border-left-color: var(--border-strong); }
.health-signal span { display: block; color: var(--text-muted); font-size: 0.72rem; }
.health-signal strong { display: block; margin-top: 2px; overflow-wrap: anywhere; }
.dashboard-section { margin-top: 24px; }
.dashboard-section > header, .section-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.dashboard-section > header h2, .section-heading h2 { margin: 0; }
.dashboard-section > header p, .section-heading p { margin: 0; color: var(--text-muted); font-size: 0.82rem; }
.dashboard-kpis { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.dashboard-kpis .metric { min-height: 104px; justify-content: flex-start; border-top: 2px solid var(--border-strong); }
.dashboard-kpis .metric strong { font-size: clamp(1.45rem, 2.5vw, 2rem); }
.dashboard-kpis .usage-metric { grid-column: span 1; border-color: rgba(139, 92, 246, 0.42); background: linear-gradient(145deg, var(--accent-soft), transparent 72%), var(--bg-surface); }
.dashboard-kpis .usage-metric strong { color: #ded5ff; }
.quick-grid.secondary-actions { grid-template-columns: repeat(auto-fit, minmax(min(100%, 190px), 1fr)); gap: 9px; }
.secondary-actions .quick-card { padding: 12px 14px; background: transparent; }

.operations-overall { position: relative; padding: 20px; border-radius: var(--radius-lg); box-shadow: var(--shadow-card); }
.operations-overall strong { font-size: clamp(1.15rem, 2vw, 1.5rem); }
.operations-overall .badge { flex: 0 0 auto; }
.overall-copy { min-width: 0; }
.overall-copy span { display: block; margin-top: 3px; }
.component-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; }
.component-card { margin: 0; border-left-width: 3px; }
.component-card .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.component-card .metric { padding: 10px 11px; background: var(--bg-surface-raised); }
.diagnostics { margin-top: 13px; padding-top: 12px; border-top: 1px solid var(--border-subtle); }
.diagnostics-label { color: var(--text-muted); font-size: 0.7rem; font-weight: 730; letter-spacing: 0.08em; text-transform: uppercase; }
.diagnostics .reasons { margin-top: 7px; }
.technical-strip { margin: 14px 0 24px; padding: 12px 15px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: rgba(20, 22, 32, 0.62); }
.technical-strip h2 { margin: 0 0 9px; color: var(--text-muted); font-size: 0.78rem; letter-spacing: 0.08em; text-transform: uppercase; }
.metadata-list { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 9px 16px; }
.metadata-list div { min-width: 0; }
.metadata-list dt { font-size: 0.7rem; }
.metadata-list dd { margin-top: 2px; color: var(--text-secondary); font-size: 0.82rem; overflow-wrap: anywhere; }
.history-section > .section-heading:first-child { margin-bottom: 13px; }
.availability-window > header { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
.availability-window > header h3 { margin: 0; }
.availability-primary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.availability-primary .metric { padding: 10px; background: var(--bg-surface); }
.availability-primary .metric strong { font-size: 1.08rem; }
.availability-primary .metric.key-metric { border-color: rgba(139, 92, 246, 0.3); }
.availability-note { margin: 12px 0 0; padding: 9px 11px; border-left: 2px solid var(--warning); background: rgba(245, 189, 88, 0.055); color: var(--text-secondary); font-size: 0.8rem; }
.availability-note.complete { border-left-color: var(--success); background: rgba(67, 209, 158, 0.045); }
details.availability-details { margin-top: 12px; border-top: 1px solid var(--border-subtle); padding-top: 10px; }
details.availability-details summary { width: fit-content; color: var(--text-secondary); cursor: pointer; font-size: 0.82rem; }
details.availability-details summary:hover { color: var(--text-primary); }
.detail-metadata { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 16px; margin-top: 11px; }
.detail-metadata div { display: flex; justify-content: space-between; gap: 12px; min-width: 0; padding-bottom: 5px; border-bottom: 1px solid rgba(65, 70, 93, 0.45); }
.detail-metadata dt { font-size: 0.76rem; }
.detail-metadata dd { color: var(--text-secondary); font-size: 0.76rem; text-align: right; }
.incident-list { position: relative; }
.incident { position: relative; grid-template-columns: minmax(140px, 0.7fr) minmax(180px, 1.4fr) auto; align-items: start; gap: 8px 15px; border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
.incident.healthy { border-left-color: var(--success); }
.incident.degraded { border-left-color: var(--warning); }
.incident.unavailable { border-left-color: var(--danger); }
.incident small { text-align: right; overflow-wrap: anywhere; }
.calm-empty { margin: 0; padding: 13px 15px; border: 1px solid rgba(67, 209, 158, 0.2); border-radius: var(--radius-sm); background: rgba(67, 209, 158, 0.045); color: var(--text-secondary); }
.integrity-summary { display: flex; align-items: center; gap: 10px; color: var(--text-secondary); }
.integrity-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.integrity-list li { min-width: 0; padding: 9px 11px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-surface-raised); }
.integrity-list li + li { margin-top: 0; }

.visually-hidden {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}
.analytics-period-selector { display: flex; align-items: center; gap: 8px; }
.analytics-period-selector .button { min-width: 92px; min-height: 42px; }
.analytics-period-selector .button[aria-current="page"] {
  border-color: rgba(167, 139, 250, 0.68);
  background: var(--accent);
  color: white;
  box-shadow: var(--shadow-neon-soft);
}
.analytics-context {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 24px;
  padding: 16px;
  border: 1px solid rgba(139, 92, 246, 0.28);
  border-radius: var(--radius-lg);
  background: linear-gradient(120deg, rgba(139, 92, 246, 0.1), transparent 58%), var(--bg-surface);
  box-shadow: var(--shadow-card);
}
.analytics-context > div { min-width: 0; padding: 7px 10px; }
.analytics-context span { display: block; color: var(--text-muted); font-size: 0.72rem; }
.analytics-context strong { display: block; margin-top: 2px; overflow-wrap: anywhere; }
.analytics-context > p { grid-column: 1 / -1; margin: 2px 10px 0; padding-top: 11px; border-top: 1px solid var(--border-subtle); color: var(--text-secondary); font-size: 0.82rem; }
.analytics-section { margin-top: 25px; }
.analytics-panel {
  min-width: 0;
  padding: 18px;
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-lg);
  background: var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.coverage-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.coverage-source { min-width: 0; padding: 15px; border: 1px solid var(--border-subtle); border-left: 3px solid var(--info); border-radius: var(--radius-md); background: var(--bg-surface); }
.coverage-source h3 { margin-bottom: 9px; }
.coverage-earliest { display: grid; gap: 2px; }
.coverage-label { color: var(--text-muted); font-size: 0.74rem; }
.coverage-earliest time, .coverage-empty { font-size: 1rem; }
.coverage-flags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }
.coverage-flags .badge { white-space: normal; line-height: 1.25; }
.coverage-method { margin: 10px 0 0; color: var(--text-muted); font-size: 0.8rem; }
.analytics-kpi-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 11px; }
.analytics-kpi {
  min-width: 0;
  display: flex;
  flex-direction: column;
  padding: 16px;
  border: 1px solid var(--border-subtle);
  border-top: 2px solid rgba(139, 92, 246, 0.52);
  border-radius: var(--radius-md);
  background: linear-gradient(150deg, rgba(139, 92, 246, 0.075), transparent 58%), var(--bg-surface);
  box-shadow: var(--shadow-sm);
}
.analytics-kpi-label { margin: 0; color: var(--text-muted); font-size: 0.76rem; }
.analytics-kpi-value { margin-top: 4px; color: var(--text-primary); font-size: clamp(1.3rem, 2vw, 1.75rem); overflow-wrap: anywhere; }
.kpi-secondary { margin: 3px 0 0; color: #cbbcff; font-size: 0.78rem; }
.kpi-caveat, .comparison-caveat { margin: 8px 0 0; padding-left: 8px; border-left: 2px solid var(--border-strong); color: var(--text-secondary); font-size: 0.73rem; line-height: 1.35; }
.kpi-caveat.warning, .comparison-caveat.warning { border-left-color: var(--warning); }
.analytics-comparison { display: grid; gap: 4px; margin-top: auto; padding-top: 13px; }
.analytics-comparison > span:first-child { color: var(--text-muted); font-size: 0.68rem; }
.delta-state { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; font-size: 0.8rem; font-weight: 680; }
.delta-state.positive { color: var(--success); }
.delta-state.negative { color: var(--danger); }
.delta-state.neutral { color: var(--text-secondary); }
.delta-percent { font-size: 0.72rem; }
.analytics-chart-scroll { max-width: 100%; overflow-x: auto; padding: 4px 2px 8px; overscroll-behavior-inline: contain; }
.daily-chart {
  min-width: 0;
  height: 245px;
  display: grid;
  grid-template-columns: repeat(7, minmax(46px, 1fr));
  align-items: end;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.daily-chart-30 { min-width: 1080px; grid-template-columns: repeat(30, minmax(28px, 1fr)); gap: 5px; }
.daily-chart-90 { min-width: 2700px; grid-template-columns: repeat(90, minmax(24px, 1fr)); gap: 4px; }
.daily-chart-games .daily-bar-fill { background: linear-gradient(180deg, var(--accent-pink), #805ad5); }
.daily-point { min-width: 0; height: 100%; display: flex; flex-direction: column; justify-content: flex-end; align-items: center; gap: 5px; text-align: center; }
.daily-value { min-height: 2.6em; display: flex; align-items: end; color: var(--text-secondary); font-size: 0.7rem; line-height: 1.25; }
.daily-estimated { max-width: 100%; color: #b7a7ee; font-size: 0.61rem; line-height: 1.2; overflow-wrap: anywhere; }
.daily-bar-track { width: min(100%, 44px); height: 145px; display: flex; align-items: flex-end; overflow: hidden; border: 1px solid var(--border-subtle); border-radius: 6px 6px 3px 3px; background: var(--bg-input); }
.daily-bar-fill { width: 100%; height: var(--bar-height); min-height: 0; border-radius: 5px 5px 2px 2px; background: linear-gradient(180deg, var(--accent-pink), var(--accent)); }
.daily-chart-messages .daily-bar-fill { background: linear-gradient(180deg, var(--info), #357eac); }
.daily-point time { color: var(--text-muted); font-size: 0.68rem; white-space: nowrap; }
.analytics-chart-empty { margin-bottom: 12px; }
.activity-summary { display: grid; grid-template-columns: 1.4fr 1fr 1fr; gap: 10px; margin-bottom: 15px; }
.activity-summary > div { min-width: 0; padding: 12px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-surface-raised); }
.activity-summary span { display: block; color: var(--text-muted); font-size: 0.7rem; }
.activity-summary strong { display: block; margin-top: 5px; color: var(--text-secondary); font-size: 0.84rem; overflow-wrap: anywhere; }
.activity-hours { display: flex !important; gap: 5px; flex-wrap: wrap; }
.activity-estimated-note { margin: 0 0 14px; font-size: 0.8rem; }
.heatmap-scroll { max-width: 100%; overflow-x: auto; overscroll-behavior-inline: contain; }
.activity-heatmap { min-width: 590px; table-layout: fixed; }
.activity-heatmap th, .activity-heatmap td { padding: 8px; text-align: center; vertical-align: middle; }
.activity-heatmap th:first-child { width: 78px; }
.heatmap-cell { border-left: 3px solid var(--bg-surface); border-right: 3px solid var(--bg-surface); border-radius: 6px; font-size: 1rem; }
.heatmap-cell.level-0 { background: #10121a; color: #514f60; }
.heatmap-cell.level-1 { background: rgba(139, 92, 246, 0.14); color: #a28be8; }
.heatmap-cell.level-2 { background: rgba(139, 92, 246, 0.28); color: #c0aff6; }
.heatmap-cell.level-3 { background: rgba(139, 92, 246, 0.48); color: #e0d7ff; }
.heatmap-cell.level-4 { background: rgba(232, 121, 249, 0.65); color: white; box-shadow: inset 0 0 12px rgba(255, 255, 255, 0.1); }
.analytics-rankings { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 13px; margin-top: 25px; }
.ranking-list { margin: 0; padding: 0; list-style: none; }
.ranking-item { display: grid; grid-template-columns: 30px minmax(0, 1fr) auto; align-items: center; gap: 9px 12px; padding: 11px 9px; border-bottom: 1px solid var(--border-subtle); }
.ranking-item:last-child { border-bottom: 0; }
.ranking-position { width: 28px; height: 28px; display: grid; place-items: center; border: 1px solid rgba(139, 92, 246, 0.35); border-radius: 50%; background: var(--accent-soft); color: #d7cdff; font-size: 0.76rem; font-weight: 760; }
.ranking-member { min-width: 0; display: grid; }
.ranking-member a { width: fit-content; max-width: 100%; color: var(--text-primary); font-weight: 650; overflow-wrap: anywhere; }
.ranking-member code { margin-top: 2px; color: var(--text-muted); font-size: 0.68rem; }
.ranking-item > strong { text-align: right; white-space: nowrap; }
.ranking-item > small { grid-column: 2 / -1; margin-top: -7px; color: #b7a7ee; font-size: 0.7rem; }
.server-game-coverage { grid-template-columns: minmax(0, 1fr); }
.server-game-kpi-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.server-game-ranking-item .ranking-member strong { overflow-wrap: anywhere; }
.server-game-ranking-item .ranking-member small { margin-top: 3px; color: var(--text-muted); font-size: 0.7rem; overflow-wrap: anywhere; }
.server-game-ranking-item > small { text-align: right; }
.server-games-empty { margin-top: 20px; }
.analytics-methodology { margin-top: 25px; }
.analytics-methodology h2 { margin: 0 0 8px; font-size: 1rem; }
.analytics-methodology p { margin: 6px 0; }

@media (max-width: 1100px) {
  .member-toolbar { grid-template-columns: minmax(220px, 1fr) repeat(2, minmax(145px, 0.55fr)); }
  .member-toolbar-actions { grid-column: 1 / -1; }
  .member-row { grid-template-columns: 50px minmax(0, 1fr) auto; align-items: start; }
  .member-stats { grid-column: 2 / -1; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .member-profile-action { grid-column: 3; grid-row: 1; }
}
@media (max-width: 900px) {
  .app-shell { display: block; }
  .sidebar { position: static; width: 100%; height: auto; max-height: none; gap: 16px; padding: 14px 16px; border-right: 0; border-bottom: 1px solid var(--border-subtle); }
  .desktop-navigation-shell { display: none; }
  .mobile-menu { display: block; width: 100%; }
  .mobile-menu-summary {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto auto;
    align-items: center;
    gap: 10px;
    min-height: 48px;
    padding: 2px 3px;
    border-radius: var(--radius-sm);
    cursor: pointer;
    list-style: none;
  }
  .mobile-menu-summary::-webkit-details-marker { display: none; }
  .mobile-menu-summary::marker { content: ""; }
  .mobile-menu-summary:hover { background: rgba(255, 255, 255, 0.025); }
  .mobile-brand { display: flex; align-items: center; gap: 9px; min-width: 0; }
  .mobile-brand .brand-mark { width: 32px; height: 32px; flex: 0 0 auto; }
  .mobile-brand .brand-copy { min-width: 0; }
  .mobile-menu-label {
    display: inline-flex;
    align-items: center;
    min-height: 34px;
    padding: 5px 9px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    font-size: 0.78rem;
    font-weight: 680;
  }
  .mobile-menu-label::after { content: "▾"; margin-left: 6px; color: var(--accent-hover); }
  .mobile-menu[open] .mobile-menu-label::after { content: "▴"; }
  .mobile-menu-panel { padding: 12px 0 3px; border-top: 1px solid var(--border-subtle); }
  .navigation { grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px 14px; }
  .mobile-navigation { gap: 12px; }
  .mobile-navigation .nav-link { min-height: 44px; }
  .mobile-menu-footer { margin-top: 12px; padding: 10px 10px 0; border-top: 1px solid var(--border-subtle); }
  .mobile-menu-footer .logout button { min-height: 44px; }
  .sidebar-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 0; padding: 10px 8px 0; }
  .session-meta { margin: 0; }
  .logout button { width: auto; }
  .content { padding-top: 26px; }
  .dashboard-hero { grid-template-columns: 1fr; }
  .metadata-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .analytics-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .server-game-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .analytics-rankings { grid-template-columns: 1fr; }
  .member-profile-action { grid-column: 1 / -1; grid-row: auto; width: 100%; }
}
@media (min-width: 1200px) {
  .cards { grid-template-columns: repeat(4, minmax(0, 1fr)); }
}
@media (max-width: 700px) {
  .cards { grid-template-columns: 1fr; }
  .component-grid { grid-template-columns: 1fr; }
  .dashboard-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .dashboard-kpis .usage-metric { grid-column: span 1; }
  .incident { grid-template-columns: 1fr; }
  .incident small { text-align: left; }
  .analytics-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .member-activity-kpis { grid-template-columns: 1fr; }
  .member-games-kpis, .member-games-details { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  body { font-size: 14px; }
  .sidebar { gap: 0; padding: 10px 13px; }
  .navigation { grid-template-columns: 1fr 1fr; }
  .responsive-desktop-only { display: none !important; }
  .responsive-mobile-only { display: block; }
  .content { padding: 20px 13px 42px; }
  .page-header { align-items: flex-start; flex-direction: column; gap: 12px; margin-bottom: 18px; padding-bottom: 15px; }
  .page-actions { width: 100%; }
  .search { align-items: stretch; flex-direction: column; }
  .search button { width: 100%; }
  .metrics { grid-template-columns: 1fr; }
  .profile-grid { grid-template-columns: 1fr; }
  .avatar { width: 112px; height: 112px; }
  dl { grid-template-columns: 1fr; gap: 2px; }
  dd + dt { margin-top: 8px; }
  .overall { align-items: flex-start; flex-direction: column; }
  .hero-statuses, .availability-primary { grid-template-columns: 1fr; }
  .detail-metadata, .integrity-list { grid-template-columns: 1fr; }
  .dashboard-section > header, .section-heading { align-items: flex-start; flex-direction: column; gap: 4px; }
  .record-action { min-height: 40px; display: inline-flex; align-items: center; }
  .record-footer button { width: 100%; min-height: 44px; }
  .analytics-period-selector { width: 100%; }
  .analytics-period-selector .button { flex: 1 1 0; min-height: 44px; }
  .analytics-context, .coverage-grid, .activity-summary { grid-template-columns: 1fr; }
  .server-game-kpi-grid { grid-template-columns: 1fr; }
  .analytics-context > p { grid-column: auto; }
  .analytics-panel { padding: 14px; }
  .daily-chart-7 { min-width: 560px; }
  .ranking-item { grid-template-columns: 30px minmax(0, 1fr) auto; }
  .member-directory-summary { align-items: flex-start; flex-direction: column; gap: 2px; }
  .member-toolbar { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 12px; }
  .member-search-field, .member-toolbar-actions { grid-column: 1 / -1; }
  .member-toolbar-actions > * { flex: 1 1 0; }
  .member-row { grid-template-columns: 46px minmax(0, 1fr); padding: 12px; }
  .member-monogram, .member-avatar { width: 42px; height: 42px; }
  .member-stats { grid-column: 1 / -1; grid-template-columns: repeat(2, minmax(0, 1fr)); padding-top: 10px; border-top: 1px solid var(--border-subtle); }
  .member-pagination { display: grid; grid-template-columns: 1fr auto 1fr; }
  .member-pagination .button { min-width: 0; }
  .profile-hero { grid-template-columns: 78px minmax(0, 1fr); align-items: start; padding: 15px; }
  .profile-monogram, .profile-avatar { width: 72px; height: 72px; font-size: 1.2rem; }
  .profile-status { grid-column: 2; }
  .profile-identity, .profile-membership, .profile-stat-grid, .achievement-list { grid-template-columns: 1fr; }
  .member-activity-header { align-items: stretch; flex-direction: column; }
  .member-games-header { align-items: stretch; flex-direction: column; }
  .member-activity-table, .member-activity-table tbody { display: block; }
  .member-activity-table thead { display: none; }
  .member-activity-table tbody { display: grid; gap: 8px; padding: 8px; }
  .member-activity-table tr { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 10px; border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); background: var(--bg-surface-raised); }
  .member-activity-table td { display: grid; width: auto; padding: 5px; border: 0; }
  .member-activity-table td:first-child { grid-column: 1 / -1; padding-bottom: 8px; border-bottom: 1px solid var(--border-subtle); }
  .member-activity-table td::before { content: attr(data-label); color: var(--text-muted); font-size: 0.66rem; font-weight: 760; letter-spacing: 0.05em; text-transform: uppercase; }
}
@media (max-width: 430px) {
  .navigation { grid-template-columns: 1fr; }
  .mobile-menu-summary { grid-template-columns: minmax(0, 1fr) auto auto; gap: 7px; }
  .mobile-menu-summary .role-badge { font-size: 0.68rem; }
  .mobile-menu-label { padding-inline: 7px; }
  .dashboard-kpis, .metadata-list { grid-template-columns: 1fr; }
  .record-header { flex-direction: column; gap: 7px; }
  .record-fields { grid-template-columns: 1fr; }
  .analytics-kpi-grid { grid-template-columns: 1fr; }
  .server-game-kpi-grid { grid-template-columns: 1fr; }
  .analytics-kpi { padding: 14px; }
  .daily-chart { height: 225px; }
  .daily-chart-7 { min-width: 500px; }
  .daily-value { font-size: 0.65rem; }
  .ranking-item { grid-template-columns: 28px minmax(0, 1fr); }
  .ranking-item > strong { grid-column: 2; text-align: left; }
  .ranking-item > small { grid-column: 2; margin-top: -4px; }
  .member-toolbar { grid-template-columns: 1fr; }
  .member-search-field, .member-toolbar-actions { grid-column: auto; }
  .member-toolbar-actions { align-items: stretch; flex-direction: column; }
  .member-stats { grid-template-columns: 1fr; }
  .member-pagination { grid-template-columns: 1fr 1fr; }
  .pagination-position { grid-column: 1 / -1; grid-row: 1; justify-self: center; }
  .profile-hero { grid-template-columns: 1fr; }
  .profile-monogram, .profile-avatar { width: 68px; height: 68px; }
  .profile-status { grid-column: auto; }
  .profile-primary h2 { font-size: 1.4rem; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; transition-duration: 0.01ms !important; }
}
"""


_NAVIGATION = (
    (
        "Обзор",
        (("/admin/", "Dashboard"),),
    ),
    (
        "Сервер",
        (
            ("/admin/members", "Участники"),
            ("/admin/analytics", "Analytics"),
            ("/admin/games", "Игры"),
            ("/admin/server-settings", "Настройки сервера"),
            ("/admin/rules", "Правила"),
        ),
    ),
    (
        "Операции",
        (
            ("/admin/system", "Состояние"),
            ("/admin/settings/bot-profile", "Профиль бота"),
        ),
    ),
)


def _is_active(href: str, active_path: str) -> bool:
    if href == "/admin/":
        return active_path == href
    return active_path == href or active_path.startswith(f"{href}/")


def _nav_link(href: str, label: str, active_path: str) -> str:
    current = ' aria-current="page"' if _is_active(href, active_path) else ""
    return f'<a class="nav-link"{current} href="{href}">{escape(label)}</a>'


def _logout_form(csrf_token: str) -> str:
    escaped_csrf = escape(csrf_token, quote=True)
    return (
        '<form method="post" action="/admin/logout" class="logout">'
        f'<input type="hidden" name="csrf_token" value="{escaped_csrf}">'
        '<button type="submit">Выйти из панели</button></form>'
    )


def render_navigation(
    role: WebAdminRole,
    csrf_token: str,
    *,
    active_path: str = "/admin/",
) -> str:
    """Render role-aware sidebar navigation with a single active destination."""

    groups = []
    for label, links in _NAVIGATION:
        rendered_links = "".join(
            _nav_link(href, title, active_path) for href, title in links
        )
        groups.append(
            '<div class="nav-group">'
            f'<span class="nav-label">{escape(label)}</span>{rendered_links}</div>'
        )
    if role is WebAdminRole.OWNER:
        groups.append(
            '<div class="nav-group"><span class="nav-label">Администрирование</span>'
            f"{_nav_link('/admin/administrators', 'Администраторы', active_path)}"
            f"{_nav_link('/admin/audit', 'Журнал аудита', active_path)}</div>"
        )
    role_label = "OWNER" if role is WebAdminRole.OWNER else "ADMIN"
    rendered_groups = "".join(groups)
    logout_form = _logout_form(csrf_token)
    return f"""<aside class="sidebar"><div class="desktop-navigation-shell">
<a class="brand" href="/admin/"><span class="brand-mark" aria-hidden="true">K</span><span class="brand-copy"><strong>Kanami</strong><small>Web Admin</small></span></a>
<nav class="navigation desktop-navigation" aria-label="Основная навигация">{rendered_groups}</nav>
<div class="sidebar-footer"><div class="session-meta"><span>Текущая роль</span><span class="role-badge">{role_label}</span></div>
{logout_form}</div></div>
<details class="mobile-menu"><summary class="mobile-menu-summary"><span class="mobile-brand"><span class="brand-mark" aria-hidden="true">K</span><span class="brand-copy"><strong>Kanami</strong><small>Web Admin</small></span></span><span class="role-badge">{role_label}</span><span class="mobile-menu-label">Меню</span></summary>
<div class="mobile-menu-panel"><nav class="navigation mobile-navigation" aria-label="Мобильная навигация">{rendered_groups}</nav>
<div class="mobile-menu-footer">{logout_form}</div></div></details>
</aside>"""


def render_admin_page(
    title: str,
    body: str,
    *,
    role: WebAdminRole,
    csrf_token: str,
    active_path: str,
    description: str | None = None,
    actions: str = "",
    wide: bool = False,
    kicker: str = "Kanami Control",
) -> str:
    """Wrap escaped page metadata and trusted server-rendered body in the app shell."""

    description_html = (
        f'<p class="page-description">{escape(description)}</p>' if description else ""
    )
    actions_html = f'<div class="page-actions">{actions}</div>' if actions else ""
    width_class = " wide" if wide else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Kanami Admin</title><style>{ADMIN_STYLES}</style></head>
<body><div class="app-shell">{render_navigation(role, csrf_token, active_path=active_path)}
<div class="workspace"><main class="content{width_class}"><header class="page-header"><div class="page-heading">
<p class="page-kicker">{escape(kicker)}</p><h1>{escape(title)}</h1>{description_html}</div>{actions_html}</header>
{body}</main></div></div></body></html>"""
