"use client";
import { useState, useEffect, useRef } from "react";
import { listProducts } from "@/lib/api";

export type AutocompleteProduct = {
  id: string;
  site_id: string;
  external_id: string | null;
  name: string;
  brand: string | null;
  category: string | null;
  device_type: string | null;
  description: string | null;
  url: string | null;
};

interface Props {
  value: string;
  onChange: (val: string) => void;
  onSelect?: (product: AutocompleteProduct) => void;
  licenseKey: string;
  placeholder?: string;
  style?: React.CSSProperties;
  className?: string;
  deviceTypes?: string[];
  required?: boolean;
}

const DEVICE_BADGE_COLORS: Record<string, string> = {
  bjt: "#f59e0b",
  mosfet: "#10b981",
  diode: "#8b5cf6",
  ic_linear: "#06b6d4",
  ic_logic: "#6366f1",
  ic_power: "#ef4444",
};

function DeviceBadge({ type }: { type: string | null }) {
  if (!type) return null;
  const color = DEVICE_BADGE_COLORS[type] || "#64748b";
  return (
    <span
      style={{
        fontSize: "0.6rem",
        fontFamily: "'DM Mono', monospace",
        padding: "1px 6px",
        borderRadius: "3px",
        background: `${color}22`,
        color,
        border: `1px solid ${color}55`,
        textTransform: "uppercase",
        letterSpacing: "0.05em",
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      {type.replace("ic_", "")}
    </span>
  );
}

export default function MpnAutocomplete({
  value,
  onChange,
  onSelect,
  licenseKey,
  placeholder,
  style,
  className,
  deviceTypes,
  required,
}: Props) {
  const [suggestions, setSuggestions] = useState<AutocompleteProduct[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [highlighted, setHighlighted] = useState(-1);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastPickedRef = useRef<string>("");

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const trimmed = value.trim();
    // Skip fetch immediately after user picks a suggestion
    if (trimmed === lastPickedRef.current) return;
    if (trimmed.length < 2) {
      setSuggestions([]);
      setOpen(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await listProducts(
          { q: trimmed, per_page: 8 },
          licenseKey
        );
        let items: AutocompleteProduct[] = res.products || [];
        if (deviceTypes && deviceTypes.length > 0) {
          items = items.filter((p) =>
            deviceTypes.includes(p.device_type || "")
          );
        }
        setSuggestions(items);
        setOpen(items.length > 0);
        setHighlighted(-1);
      } catch {
        setSuggestions([]);
        setOpen(false);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, [value, licenseKey, deviceTypes]);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const pick = (p: AutocompleteProduct) => {
    const mpn = p.external_id || p.name.split(/\s/)[0];
    lastPickedRef.current = mpn;
    onChange(mpn);
    onSelect?.(p);
    setOpen(false);
    setHighlighted(-1);
  };

  const handleKey = (e: React.KeyboardEvent) => {
    if (!open || suggestions.length === 0) {
      if (e.key === "ArrowDown" && suggestions.length > 0) {
        setOpen(true);
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlighted((h) => (h + 1) % suggestions.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlighted((h) => (h - 1 + suggestions.length) % suggestions.length);
    } else if (e.key === "Enter" && highlighted >= 0) {
      e.preventDefault();
      pick(suggestions[highlighted]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div ref={wrapperRef} style={{ position: "relative" }}>
      <input
        type="text"
        value={value}
        onChange={(e) => {
          lastPickedRef.current = "";
          onChange(e.target.value);
        }}
        onKeyDown={handleKey}
        onFocus={() => {
          if (suggestions.length > 0) setOpen(true);
        }}
        placeholder={placeholder}
        style={style}
        className={className}
        autoComplete="off"
        required={required}
      />
      {loading && (
        <span
          style={{
            position: "absolute",
            right: "0.75rem",
            top: "50%",
            transform: "translateY(-50%)",
            fontSize: "0.65rem",
            color: "var(--text-dim)",
            fontFamily: "'DM Mono', monospace",
            letterSpacing: "0.05em",
            pointerEvents: "none",
          }}
        >
          검색중
        </span>
      )}
      {open && suggestions.length > 0 && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 4px)",
            left: 0,
            right: 0,
            background: "var(--bg-panel)",
            border: "1px solid var(--border-dim)",
            borderRadius: "0.5rem",
            boxShadow: "0 8px 24px rgba(0,0,0,0.45)",
            maxHeight: "340px",
            overflowY: "auto",
            zIndex: 50,
          }}
        >
          {suggestions.map((p, i) => {
            const mpn = p.external_id || p.name.split(/\s/)[0];
            return (
              <div
                key={p.id}
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(p);
                }}
                onMouseEnter={() => setHighlighted(i)}
                style={{
                  padding: "0.55rem 0.75rem",
                  cursor: "pointer",
                  background:
                    highlighted === i
                      ? "rgba(0,200,240,0.08)"
                      : "transparent",
                  borderBottom:
                    i < suggestions.length - 1
                      ? "1px solid var(--border-dim)"
                      : "none",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.2rem",
                }}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: "0.5rem",
                    justifyContent: "space-between",
                  }}
                >
                  <span
                    style={{
                      fontSize: "0.82rem",
                      color: "var(--text-primary)",
                      fontFamily: "'DM Mono', monospace",
                      fontWeight: 600,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {mpn}
                  </span>
                  <DeviceBadge type={p.device_type} />
                </div>
                <div
                  style={{
                    fontSize: "0.68rem",
                    color: "var(--text-dim)",
                    display: "flex",
                    gap: "0.4rem",
                    alignItems: "center",
                  }}
                >
                  <span>{p.brand || "—"}</span>
                  <span style={{ opacity: 0.5 }}>·</span>
                  <span
                    style={{
                      textTransform: "uppercase",
                      letterSpacing: "0.05em",
                      fontFamily: "'DM Mono', monospace",
                    }}
                  >
                    {p.site_id}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
