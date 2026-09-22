<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { DesktopBridge } from "@/api/bridge";
import type { Notification, ResponseOption } from "@/model/protocol";
import MarkdownContent from "@/components/MarkdownContent.vue";
import { identityAccentIndex } from "@/presentation/identity";

const props = defineProps<{
  notification: Notification;
  bridge: DesktopBridge;
  pending: boolean;
  error?: { message: string; retryable: boolean };
  connected: boolean;
  now: number;
  rawMarkdown?: boolean;
  selected?: boolean;
}>();
const emit = defineEmits<{
  read: [notification: Notification, read: boolean];
  respond: [notification: Notification, option: ResponseOption, message: string | null];
  select: [notification: Notification];
}>();

const card = ref<HTMLElement | null>(null);
const detailsOpen = ref(false);
const responseMessage = ref("");
const validationError = ref<string | null>(null);
const raw = ref(props.rawMarkdown ?? false);
const rawLocallyToggled = ref(false);
const domainAccent = computed(() => identityAccentIndex(props.notification.domain));
const senderAccent = computed(() => identityAccentIndex(props.notification.sender));
const hasMarkdown = computed(() => Boolean(props.notification.message) || props.notification.details !== null);
const hasMessageField = computed(() => props.notification.response_options.some((option) => option.message_mode !== "none"));
const age = computed(() => {
  const seconds = Math.max(0, Math.floor((props.now - Date.parse(props.notification.created_at)) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
});
const responseLabel = computed(() => {
  const response = props.notification.response;
  if (!response) return props.notification.response_state === "cancelled" ? "Response cancelled" : "Response expired";
  const option = props.notification.response_options.find((item) => item.id === response.option_id);
  return `Answered: ${option?.label ?? response.option_id}`;
});

watch(() => props.notification.response_state, (state) => {
  if (state !== "pending") responseMessage.value = "";
});
watch(() => props.rawMarkdown, (value) => {
  if (!rawLocallyToggled.value) raw.value = value ?? false;
});
watch(() => props.notification.id, () => {
  detailsOpen.value = false;
  responseMessage.value = "";
  validationError.value = null;
  rawLocallyToggled.value = false;
  raw.value = props.rawMarkdown ?? false;
});

function choose(option: ResponseOption): void {
  validationError.value = null;
  if (option.message_mode === "none") {
    emit("respond", props.notification, option, null);
    return;
  }
  if (responseMessage.value.length > 16 * 1024) {
    validationError.value = "The response message must be at most 16 KiB.";
    return;
  }
  emit("respond", props.notification, option, responseMessage.value || null);
}

function responseModeLabel(option: ResponseOption): string {
  if (option.message_mode === "none") return "Ignores the response message";
  if (option.message_mode === "optional") return "Uses the response message when provided";
  return responseMessage.value.length === 0
    ? "Requires a response message; enter a message to enable this choice"
    : "Requires and sends the response message";
}

function focusCard(scroll = true): void {
  card.value?.focus({ preventScroll: true });
  if (scroll) card.value?.scrollIntoView?.({ block: "nearest" });
}

function toggleDetails(): boolean {
  if (props.notification.details === null) return false;
  detailsOpen.value = !detailsOpen.value;
  return true;
}

function toggleRead(): boolean {
  if (props.pending || !props.connected) return false;
  emit("read", props.notification, props.notification.read_at === null);
  return true;
}

function cancelResponseEditor(): void {
  responseMessage.value = "";
  validationError.value = null;
  focusCard(false);
}

function handleResponseEscape(event: KeyboardEvent): void {
  if (event.key !== "Escape" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey) return;
  event.stopPropagation();
  event.preventDefault();
  cancelResponseEditor();
}

defineExpose({ focusCard, toggleDetails, toggleRead });
</script>

<template>
  <article
    ref="card"
    :class="{ unread: notification.read_at === null, selected }"
    :aria-labelledby="`summary-${notification.id}`"
    :aria-current="selected ? 'true' : undefined"
    :data-notification-id="notification.id"
    :tabindex="selected ? 0 : -1"
    @click="emit('select', notification)"
    @focusin="emit('select', notification)"
  >
    <div class="card-heading">
      <div class="metadata">
        <span class="priority" :data-priority="notification.priority">{{ notification.priority }}</span>
        <span class="identity-label domain-label" :data-identity-accent="domainAccent">{{ notification.domain }}</span> ·
        <span class="identity-label sender-label" :data-identity-accent="senderAccent">{{ notification.sender }}</span> ·
        <time :datetime="notification.created_at">{{ age }}</time>
        <span v-if="notification.read_at === null" class="unread-label">Unread</span>
      </div>
      <div class="card-actions" aria-label="Card actions">
        <span class="icon-action">
          <button
            type="button"
            class="icon-button read-toggle"
            :aria-label="`Mark ${notification.read_at === null ? 'read' : 'unread'}`"
            :aria-describedby="`read-tooltip-${notification.id}`"
            :disabled="pending || !connected"
            @click="toggleRead"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true">
              <path v-if="notification.read_at === null" d="M3 5.5h14v9H3zM3.5 6l6.5 5 6.5-5" />
              <path v-else d="M3 5.5h14v9H3zM3.5 6l6.5 5 6.5-5M6 3.5h8" />
            </svg>
          </button>
          <span :id="`read-tooltip-${notification.id}`" class="action-tooltip" role="tooltip">Mark {{ notification.read_at === null ? "read" : "unread" }}</span>
        </span>
        <span v-if="hasMarkdown" class="icon-action">
          <button
            type="button"
            class="icon-button source-toggle"
            :aria-label="raw ? 'Show rendered content' : 'Show source content'"
            :aria-describedby="`source-tooltip-${notification.id}`"
            :aria-pressed="raw"
            @click="rawLocallyToggled = true; raw = !raw"
          >
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7 5-4 5 4 5M13 5l4 5-4 5M11.5 3.5l-3 13" /></svg>
          </button>
          <span :id="`source-tooltip-${notification.id}`" class="action-tooltip" role="tooltip">{{ raw ? "Show rendered content" : "Show source content" }}</span>
        </span>
      </div>
    </div>
    <h2 :id="`summary-${notification.id}`">{{ notification.summary }}</h2>
    <MarkdownContent v-if="notification.message" :source="notification.message" :bridge="bridge" label="message" :raw="raw" :show-toggle="false" />
    <button
      v-if="notification.details !== null"
      type="button"
      class="details-toggle"
      :aria-expanded="detailsOpen"
      :aria-controls="`details-${notification.id}`"
      @click="toggleDetails"
    >
      <span class="disclosure-chevron" aria-hidden="true">{{ detailsOpen ? "⌃" : "⌄" }}</span>
      <span class="details-label">Details</span>
      <span class="details-divider" aria-hidden="true" />
    </button>
    <div v-if="detailsOpen && notification.details !== null" :id="`details-${notification.id}`" class="details-content">
      <MarkdownContent :source="notification.details" :bridge="bridge" label="details" :raw="raw" :show-toggle="false" />
    </div>
    <div v-if="notification.tags.length" class="tags" aria-label="Tags">
      <span v-for="tag in notification.tags" :key="tag">{{ tag }}</span>
    </div>

    <section
      v-if="notification.response_state === 'pending'"
      class="response-controls"
      aria-label="Response options"
      data-response-form
      @keydown="handleResponseEscape"
    >
      <div v-if="hasMessageField" class="response-editor">
        <label class="response-message-label" :for="`response-${notification.id}`">Response Message</label>
        <textarea
          :id="`response-${notification.id}`"
          v-model="responseMessage"
          maxlength="16384"
          :disabled="pending || !connected"
          :aria-describedby="`response-message-help-${notification.id}`"
          @input="validationError = null"
        />
        <small :id="`response-message-help-${notification.id}`">Choices indicate whether they ignore, optionally use, or require this message.</small>
      </div>
      <div class="response-buttons">
        <button
          v-for="option in notification.response_options"
          :key="option.id"
          type="button"
          :class="`response-${option.appearance}`"
          :disabled="pending || !connected || (option.message_mode === 'required' && responseMessage.length === 0)"
          :aria-label="`${option.label}. ${responseModeLabel(option)}`"
          :title="responseModeLabel(option)"
          @click="choose(option)"
        >
          <span class="response-mode-icon" aria-hidden="true">
            <svg v-if="option.message_mode === 'none'" viewBox="0 0 20 20"><path d="M4 5h12v8H8l-4 3zM3 3l14 14" /></svg>
            <svg v-else-if="option.message_mode === 'optional'" viewBox="0 0 20 20"><path d="M4 4h12v9H8l-4 3zM7 7h6M7 10h4" /><circle cx="15.5" cy="15.5" r="2.5" /></svg>
            <svg v-else viewBox="0 0 20 20"><path d="M4 4h12v9H8l-4 3zM7 7h6M7 10h4M15.5 14v4M13.5 16h4" /></svg>
          </span>
          <span>{{ option.label }}</span>
        </button>
      </div>
      <p v-if="pending" class="pending" role="status">Submitting…</p>
      <p v-if="validationError" class="inline-error" role="alert">{{ validationError }}</p>
    </section>
    <section v-else-if="notification.response_state !== 'not_requested'" class="terminal-response" aria-label="Response result">
      <strong>{{ responseLabel }}</strong>
      <p v-if="notification.response?.message">{{ notification.response.message }}</p>
      <small v-if="notification.response">by {{ notification.response.responded_by }} · {{ notification.response.responded_at }}</small>
    </section>
    <p v-if="error" class="inline-error" role="alert">
      {{ error.message }}<span v-if="error.retryable"> You can retry.</span>
    </p>
  </article>
</template>
