import { defineConfig } from 'astro/config';
import react from '@astrojs/react';
import sitemap from '@astrojs/sitemap';
import tailwindcss from '@tailwindcss/vite';

const rawSite = (process.env.SITE_URL || 'https://cairnmind.dev').trim().replace(/\/+$/, '');

export default defineConfig({
  site: rawSite,
  output: 'static',
  trailingSlash: 'always',
  compressHTML: true,
  integrations: [
    react(),
    sitemap({
      filter: (page) => !page.endsWith('/404/'),
      changefreq: 'weekly',
      priority: 0.7,
      serialize(item) {
        const url = item.url;
        if (url === `${rawSite}/`) {
          return { ...item, priority: 1.0, changefreq: 'weekly' };
        }
        if (
          url.endsWith('/install/') ||
          url.endsWith('/features/') ||
          url.endsWith('/benchmarks/') ||
          url.endsWith('/docs/')
        ) {
          return { ...item, priority: 0.9, changefreq: 'weekly' };
        }
        if (url.includes('/compare/') || url.includes('/use-cases/') || url.endsWith('/faq/')) {
          return { ...item, priority: 0.85, changefreq: 'weekly' };
        }
        return item;
      },
    }),
  ],
  vite: {
    plugins: [tailwindcss()],
    build: {
      cssMinify: true,
    },
  },
  build: {
    inlineStylesheets: 'auto',
  },
});
