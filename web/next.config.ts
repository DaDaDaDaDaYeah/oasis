import type { NextConfig } from "next";

// `oasis-portal --lan` serves this dev server to other devices on the network.
// Next blocks dev-only resources (HMR, /_next/*) requested from any origin it
// doesn't know, which would leave a phone with a half-working page; allow the
// private ranges, and only when LAN mode was asked for.
const lanDevOrigins =
  process.env.OASIS_LAN === "1"
    ? ["192.168.*.*", "10.*.*.*", ...Array.from({ length: 16 }, (_, i) => `172.${16 + i}.*.*`), "*.local"]
    : [];

const nextConfig: NextConfig = {
  output: "standalone",
  ...(lanDevOrigins.length ? { allowedDevOrigins: lanDevOrigins } : {}),
};

export default nextConfig;

import('@opennextjs/cloudflare').then(m => m.initOpenNextCloudflareForDev());
