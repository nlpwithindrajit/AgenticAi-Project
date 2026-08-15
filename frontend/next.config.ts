import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Emits a self-contained server bundle so the Docker image can be a slim
  // runtime layer rather than shipping node_modules. The ECS task definition
  // sets HOSTNAME=0.0.0.0, which the standalone server needs in order to
  // accept connections from the load balancer rather than only from localhost.
  output: "standalone",
  reactStrictMode: true,
};

export default nextConfig;
