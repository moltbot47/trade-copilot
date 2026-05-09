/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // ESLint v9 dropped flags that `next lint` still uses internally —
  // disable lint-during-build. Our CI workflow runs `eslint .`
  // separately to catch issues.
  eslint: {
    ignoreDuringBuilds: true,
  },
};

module.exports = nextConfig;
