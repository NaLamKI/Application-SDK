import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist",
    rollupOptions: {
      // `/_sdk/client.js` is served by the SDK on the extension's own origin at
      // runtime; it is not part of this bundle.
      external: ["/_sdk/client.js"],
    },
  },
});
