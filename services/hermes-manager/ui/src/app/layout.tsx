import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Link, Outlet, useLocation } from "@tanstack/react-router";
import { useMutation } from "@tanstack/react-query";
import { api } from "../api";
import {
  clearPrivateState,
  queryClient,
  useAuthorizationError,
  useSession,
} from "./state";
import { themes, useTheme, type ThemePreference } from "./theme";
import { ErrorNotice } from "../components/data";
import {
  ActivityIcon,
  AdministratorsIcon,
  AssignmentsIcon,
  BackendsIcon,
  BrandMark,
  ChevronDownIcon,
  OverviewIcon,
  PersonIcon,
  RefreshIcon,
  SecretsIcon,
  SidebarIcon,
  SignOutIcon,
  StorageIcon,
  SunIcon,
  SystemIcon,
  TemplatesIcon,
  UsersIcon,
  MoonIcon,
} from "../components/icons";

const navigation = [
  [{ to: "/", label: "Overview", icon: <OverviewIcon /> }],
  [
    { to: "/users", label: "Users", icon: <UsersIcon /> },
    { to: "/assignments", label: "Agents", icon: <AssignmentsIcon /> },
    { to: "/groups", label: "Groups", icon: <UsersIcon /> },
  ],
  [
    { to: "/templates", label: "Hermes templates", icon: <TemplatesIcon /> },
    {
      to: "/terminal-backends",
      label: "Terminal backends",
      icon: <BackendsIcon />,
    },
  ],
  [
    { to: "/storage", label: "Storage", icon: <StorageIcon /> },
    { to: "/secrets", label: "Secrets", icon: <SecretsIcon /> },
  ],
  [
    { to: "/activity", label: "Activity", icon: <ActivityIcon /> },
    {
      to: "/administrators",
      label: "Administrators",
      icon: <AdministratorsIcon />,
    },
  ],
] as const;

const titles: Record<string, [string, string]> = {
  "/": [
    "Agent Platform",
    "Manage users, the instances they get, and the definitions those run on.",
  ],
  "/users": [
    "Users",
    "Exact identities known to the manager, and the optional Keycloak directory.",
  ],
  "/assignments": [
    "Agents",
    "Personal and shared instances, with their configuration and access.",
  ],
  "/groups": ["Groups", "Manage members used in shared-agent grants."],
  "/templates": [
    "Hermes templates",
    "Versioned instance definitions with their enforced admin settings.",
  ],
  "/terminal-backends": [
    "Terminal backends",
    "Versioned SSH worker definitions that assignments may allow.",
  ],
  "/storage": [
    "Storage",
    "Approved storage sources from the installation and the volumes on them.",
  ],
  "/secrets": [
    "Admin secrets",
    "Write-only values sealed to the controller. Values are never shown.",
  ],
  "/activity": ["Activity", "An attributed record of every committed change."],
  "/administrators": [
    "Administrators",
    "PostgreSQL grants for exact Keycloak identities.",
  ],
};

const SIDEBAR_KEY = "sidebar";
const narrow = "(max-width: 879px)";

function useMediaQuery(query: string) {
  return useSyncExternalStore(
    (notify) => {
      const list = window.matchMedia(query);
      list.addEventListener("change", notify);
      return () => list.removeEventListener("change", notify);
    },
    () => window.matchMedia(query).matches,
  );
}

function usePersistedCollapse() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_KEY) === "collapsed";
    } catch {
      return false;
    }
  });
  const set = (value: boolean) => {
    setCollapsed(value);
    try {
      if (value) localStorage.setItem(SIDEBAR_KEY, "collapsed");
      else localStorage.removeItem(SIDEBAR_KEY);
    } catch {
      /* The sidebar still collapses until the page is closed. */
    }
  };
  return [collapsed, set] as const;
}

const themeChoices: Record<
  ThemePreference,
  { label: string; icon: ReactNode }
> = {
  system: { label: "System", icon: <SystemIcon /> },
  light: { label: "Light", icon: <SunIcon /> },
  dark: { label: "Dark", icon: <MoonIcon /> },
};

function ThemeSwitch() {
  const [theme, setTheme] = useTheme();
  return (
    <div className="segmented" role="group" aria-label="Colour theme">
      {themes.map((choice) => (
        <button
          key={choice}
          type="button"
          aria-pressed={theme === choice}
          aria-label={themeChoices[choice].label}
          title={`${themeChoices[choice].label}${choice === "system" ? " (follows your device)" : ""}`}
          onClick={() => setTheme(choice)}
        >
          {themeChoices[choice].icon}
        </button>
      ))}
    </div>
  );
}

function AccountMenu({
  subject,
  issuer,
  disabled,
  onSignOut,
}: {
  subject: string;
  issuer: string;
  disabled: boolean;
  onSignOut: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const away = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);
  return (
    <div className="account" ref={root}>
      <button
        ref={trigger}
        type="button"
        className="account-trigger"
        aria-expanded={open}
        aria-controls="account-panel"
        onClick={() => setOpen(!open)}
      >
        <span className="avatar">
          <PersonIcon />
        </span>
        <span className="account-name">Account</span>
        <ChevronDownIcon />
      </button>
      {open && (
        <div
          className="popover"
          id="account-panel"
          role="group"
          aria-label="Account"
        >
          <dl className="identity">
            <dt>Signed in as</dt>
            <dd className="mono">{subject}</dd>
            <dt>Identity provider</dt>
            <dd className="mono">{issuer}</dd>
          </dl>
          <button
            type="button"
            className="menu-item"
            disabled={disabled}
            onClick={onSignOut}
          >
            <SignOutIcon />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

export function Layout() {
  const path = useLocation({ select: (location) => location.pathname });
  const session = useSession();
  const denied = useAuthorizationError();
  const [signingOut, setSigningOut] = useState(false);
  const [collapsed, setCollapsed] = usePersistedCollapse();
  const [drawer, setDrawer] = useState(false);
  const small = useMediaQuery(narrow);
  const logout = useMutation({
    mutationFn: async () => {
      const csrf = session.data?.csrfToken;
      if (!csrf) throw new Error("A browser session is required to sign out.");
      setSigningOut(true);
      clearPrivateState();
      await api.logout(csrf);
      window.location.replace("/auth/signed-out");
    },
    onError: () => setSigningOut(false),
  });
  useEffect(() => setDrawer(false), [path]);
  useEffect(() => {
    if (!drawer) return;
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawer(false);
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, [drawer]);

  const section = `/${path.split("/")[1]}`;
  const [title, description] = titles[section] ?? titles["/"];
  const detail = path.split("/")[2];
  const home = section === "/";
  const error = denied ?? session.error;
  const expanded = small ? drawer : !collapsed;
  const rail = !small && collapsed;
  return (
    <div className="shell" data-collapsed={rail} data-drawer={small && drawer}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar" id="sidebar">
        <Link className="brand" to="/" aria-label="Agent Platform home">
          <BrandMark size={28} />
          <span className="brand-name">Agent Platform</span>
        </Link>
        <nav aria-label="Main navigation">
          {navigation.map((group, index) => (
            <ul key={index}>
              {group.map(({ to, label, icon }) => (
                <li key={to}>
                  <Link
                    to={to}
                    title={rail ? label : undefined}
                    aria-current={
                      path === to || (to !== "/" && path.startsWith(`${to}/`))
                        ? "page"
                        : undefined
                    }
                  >
                    {icon}
                    <span className="nav-label">{label}</span>
                  </Link>
                </li>
              ))}
            </ul>
          ))}
        </nav>
      </aside>
      {small && drawer && (
        <button
          type="button"
          className="scrim"
          aria-label="Close navigation"
          onClick={() => setDrawer(false)}
        />
      )}
      <div className="workspace" inert={small && drawer}>
        <header className="topbar">
          <button
            type="button"
            className="icon-button"
            aria-label={
              small
                ? drawer
                  ? "Close navigation"
                  : "Open navigation"
                : collapsed
                  ? "Expand sidebar"
                  : "Collapse sidebar"
            }
            aria-expanded={expanded}
            aria-controls="sidebar"
            onClick={() =>
              small ? setDrawer(!drawer) : setCollapsed(!collapsed)
            }
          >
            <SidebarIcon />
          </button>
          <ol className="crumbs" aria-label="Breadcrumb">
            <li>Administration</li>
            {home ? (
              <li aria-current="page">Overview</li>
            ) : detail ? (
              <>
                <li>
                  <Link to={section as "/users"}>{title}</Link>
                </li>
                <li aria-current="page" className="mono">
                  {decodeURIComponent(detail)}
                </li>
              </>
            ) : (
              <li aria-current="page">{title}</li>
            )}
          </ol>
          <div className="topbar-end">
            <ThemeSwitch />
            {session.data && (
              <AccountMenu
                subject={session.data.actor.subject}
                issuer={session.data.actor.issuer}
                disabled={!session.data.csrfToken || logout.isPending}
                onSignOut={() => logout.mutate()}
              />
            )}
          </div>
        </header>
        <main id="main" tabIndex={-1}>
          <div className={home ? "page-heading home" : "page-heading"}>
            {home && <BrandMark size={44} />}
            <div className="page-title">
              <h1>{title}</h1>
              <p>{description}</p>
            </div>
            <button
              type="button"
              className="quiet"
              disabled={signingOut}
              onClick={() => void queryClient.invalidateQueries()}
            >
              <RefreshIcon />
              Refresh
            </button>
          </div>
          {error ? (
            <ErrorNotice error={error} />
          ) : session.isPending ? (
            <p role="status" className="loading">
              Connecting to the manager…
            </p>
          ) : (
            !signingOut && session.data && <Outlet />
          )}
          {logout.error && <ErrorNotice error={logout.error} />}
        </main>
      </div>
    </div>
  );
}
