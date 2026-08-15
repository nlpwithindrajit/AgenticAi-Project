import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits a self-contained server bundle so the Docker image can be a slim
  // runtime layer rather than shipping node_modules. Needed for App Runner.
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
