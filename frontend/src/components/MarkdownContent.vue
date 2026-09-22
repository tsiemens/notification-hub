<script setup lang="ts">
import DOMPurify from "dompurify";
import { marked } from "marked";
import { computed, ref } from "vue";

import type { DesktopBridge } from "@/api/bridge";

const props = defineProps<{ source: string; bridge: DesktopBridge; label: string }>();
const raw = ref(false);
const linkError = ref<string | null>(null);

function allowedUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password;
  } catch {
    return false;
  }
}

const rendered = computed(() => {
  const renderer = new marked.Renderer();
  renderer.html = () => "";
  const parsed = marked.parse(props.source, { async: false, gfm: true, breaks: true, renderer });
  const clean = DOMPurify.sanitize(parsed, {
    ALLOWED_TAGS: ["p", "br", "strong", "em", "del", "blockquote", "ul", "ol", "li", "code", "pre", "a", "h1", "h2", "h3", "h4"],
    ALLOWED_ATTR: ["href", "title"],
    ALLOW_DATA_ATTR: false,
  });
  const template = document.createElement("template");
  template.innerHTML = clean;
  template.content.querySelectorAll("a").forEach((link) => {
    const href = link.getAttribute("href") ?? "";
    link.removeAttribute("href");
    if (allowedUrl(href)) {
      // Do not leave a navigable href in the webview: auxiliary clicks and
      // context-menu actions do not pass through the delegated click handler.
      link.dataset.externalUrl = href;
      link.setAttribute("role", "link");
      link.setAttribute("tabindex", "0");
    }
  });
  return template.innerHTML;
});

async function activateLink(event: MouseEvent | KeyboardEvent): Promise<void> {
  const target = (event.target as Element).closest("a");
  if (!target) return;
  event.preventDefault();
  const href = (target as HTMLElement).dataset.externalUrl;
  if (!href || !allowedUrl(href)) return;
  linkError.value = null;
  try {
    const result = await props.bridge.openExternal(href);
    if (!result.ok) linkError.value = "The link could not be opened.";
  } catch {
    linkError.value = "The link could not be opened.";
  }
}

function activateLinkFromKeyboard(event: KeyboardEvent): void {
  if (event.key === "Enter" || event.key === " ") void activateLink(event);
}
</script>

<template>
  <div class="markdown-block">
    <button class="text-button raw-toggle" type="button" :aria-pressed="raw" @click="raw = !raw">
      {{ raw ? "Show rendered" : "Show source" }} for {{ label }}
    </button>
    <pre v-if="raw" class="raw-source">{{ source }}</pre>
    <!-- Audited boundary: marked has raw HTML disabled above and DOMPurify applies an explicit allowlist. -->
    <div v-else class="markdown" @click="activateLink" @keydown="activateLinkFromKeyboard" v-html="rendered" />
    <p v-if="linkError" class="inline-error" role="alert">{{ linkError }}</p>
  </div>
</template>
