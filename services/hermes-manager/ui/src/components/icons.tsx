import type { ReactNode } from "react";

/** The outline icons of the dashboard: inline, one size, drawn in the text colour. */
function Icon({ children, size = 18 }: { children: ReactNode; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export const OverviewIcon = () => (
  <Icon>
    <path d="M3 10.5 12 3l9 7.5" />
    <path d="M5 9.5V20h14V9.5" />
    <path d="M10 20v-5.5h4V20" />
  </Icon>
);
export const UsersIcon = () => (
  <Icon>
    <circle cx="9" cy="8" r="3.25" />
    <path d="M3.5 20a5.5 5.5 0 0 1 11 0" />
    <path d="M16 5.3a3.25 3.25 0 0 1 0 5.4" />
    <path d="M17.5 14.6A5.5 5.5 0 0 1 20.5 20" />
  </Icon>
);
export const AssignmentsIcon = () => (
  <Icon>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <circle cx="9" cy="11" r="2" />
    <path d="M6 16c.5-1.5 1.8-2.2 3-2.2s2.5.7 3 2.2" />
    <path d="M15 10h3M15 14h3" />
  </Icon>
);
export const TemplatesIcon = () => (
  <Icon>
    <path d="M12 3 3 8l9 5 9-5-9-5Z" />
    <path d="m3 13 9 5 9-5" />
  </Icon>
);
export const BackendsIcon = () => (
  <Icon>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="m7 9 3 3-3 3M13 15h4" />
  </Icon>
);
export const StorageIcon = () => (
  <Icon>
    <ellipse cx="12" cy="6" rx="8" ry="3" />
    <path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6" />
    <path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" />
  </Icon>
);
export const SecretsIcon = () => (
  <Icon>
    <circle cx="8" cy="15" r="4" />
    <path d="m11 12 9-9M16 7l3 3M14 9l2 2" />
  </Icon>
);
export const ActivityIcon = () => (
  <Icon>
    <path d="M3 12h4l3-8 4 16 3-8h4" />
  </Icon>
);
export const AdministratorsIcon = () => (
  <Icon>
    <path d="M12 3 4 6v6c0 4.5 3.2 8 8 9 4.8-1 8-4.5 8-9V6l-8-3Z" />
    <path d="m9 12 2 2 4-4" />
  </Icon>
);
export const SunIcon = () => (
  <Icon>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
  </Icon>
);
export const MoonIcon = () => (
  <Icon>
    <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5Z" />
  </Icon>
);
export const SystemIcon = () => (
  <Icon>
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M8 20h8M12 16v4" />
  </Icon>
);
export const SidebarIcon = () => (
  <Icon>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
  </Icon>
);
export const ChevronDownIcon = () => (
  <Icon size={16}>
    <path d="m6 9 6 6 6-6" />
  </Icon>
);
export const RefreshIcon = () => (
  <Icon size={16}>
    <path d="M21 12a9 9 0 1 1-3-6.7L21 8" />
    <path d="M21 3v5h-5" />
  </Icon>
);
export const SignOutIcon = () => (
  <Icon size={16}>
    <path d="M9 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4" />
    <path d="m16 17 5-5-5-5M21 12H9" />
  </Icon>
);
export const PersonIcon = () => (
  <Icon>
    <circle cx="12" cy="8" r="3.5" />
    <path d="M5 20a7 7 0 0 1 14 0" />
  </Icon>
);

/**
 * The platform's mark: four units on a grid, one of them live. It stands for
 * the platform as a whole, not for any one product on it.
 */
export function BrandMark({ size = 28 }: { size?: number }) {
  return (
    <svg
      className="mark"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      focusable="false"
    >
      <rect className="mark-a" x="3" y="3" width="11.5" height="11.5" rx="3" />
      <rect
        className="mark-live"
        x="17.5"
        y="3"
        width="11.5"
        height="11.5"
        rx="3"
      />
      <rect
        className="mark-b"
        x="3"
        y="17.5"
        width="11.5"
        height="11.5"
        rx="3"
      />
      <rect
        className="mark-a"
        x="17.5"
        y="17.5"
        width="11.5"
        height="11.5"
        rx="3"
      />
    </svg>
  );
}
