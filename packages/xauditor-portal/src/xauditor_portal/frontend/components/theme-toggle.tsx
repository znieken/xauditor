"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import * as React from "react";
import { Button } from "@/components/ui/button";

type Mode = "light" | "dark" | "system";
const ORDER: Mode[] = ["light", "dark", "system"];

const ICONS: Record<Mode, React.ReactNode> = {
  light: <Sun className="h-4 w-4" aria-hidden />,
  dark: <Moon className="h-4 w-4" aria-hidden />,
  system: <Monitor className="h-4 w-4" aria-hidden />,
};

export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => setMounted(true), []);
  const current = (mounted ? (theme as Mode) : "system") ?? "system";

  function cycle() {
    const idx = ORDER.indexOf(current);
    const next = ORDER[(idx + 1) % ORDER.length];
    setTheme(next);
  }

  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      aria-label={`Theme: ${current}. Click to cycle.`}
      title={`Theme: ${current}`}
      onClick={cycle}
    >
      {ICONS[current]}
    </Button>
  );
}
