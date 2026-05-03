const BACKEND_INTERNAL_URL =
  process.env.BACKEND_INTERNAL_URL || "http://xauditor-portal-backend:8000";

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${BACKEND_INTERNAL_URL}/api/:path*`,
      },
    ];
  },
  async redirects() {
    return [
      { source: "/settings", destination: "/reports", permanent: false },
      { source: "/settings/:path*", destination: "/reports", permanent: false },
    ];
  },
};

export default nextConfig;
