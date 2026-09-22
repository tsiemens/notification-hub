<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

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
}>();
const emit = defineEmits<{
  read: [notification: Notification, read: boolean];
  respond: [notification: Notification, option: ResponseOption, message: string | null];
}>();

const detailsOpen = ref(false);
const editing = ref<ResponseOption | null>(null);
const responseMessage = ref("");
const editor = ref<HTMLTextAreaElement | null>(null);
const validationError = ref<string | null>(null);
const domainAccent = computed(() => identityAccentIndex(props.notification.domain));
const senderAccent = computed(() => identityAccentIndex(props.notification.sender));
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
  if (state !== "pending") editing.value = null;
});

async function choose(option: ResponseOption): Promise<void> {
  validationError.value = null;
  if (option.message_mode === "none") {
    emit("respond", props.notification, option, null);
    return;
  }
  editing.value = option;
  responseMessage.value = "";
  await nextTick();
  editor.value?.focus();
}

function submit(): void {
  if (!editing.value) return;
  if (editing.value.message_mode === "required" && responseMessage.value.length === 0) {
    validationError.value = "A response message is required.";
    editor.value?.focus();
    return;
  }
  if (responseMessage.value.length > 16 * 1024) {
    validationError.value = "The response message must be at most 16 KiB.";
    editor.value?.focus();
    return;
  }
  emit("respond", props.notification, editing.value, responseMessage.value || null);
}
</script>

<template>
  <article
    :class="{ unread: notification.read_at === null }"
    :aria-labelledby="`summary-${notification.id}`"
    :data-notification-id="notification.id"
  >
    <div class="card-heading">
      <div class="metadata">
        <span class="priority" :data-priority="notification.priority">{{ notification.priority }}</span>
        <span class="identity-label domain-label" :data-identity-accent="domainAccent">{{ notification.domain }}</span> ·
        <span class="identity-label sender-label" :data-identity-accent="senderAccent">{{ notification.sender }}</span> ·
        <time :datetime="notification.created_at">{{ age }}</time>
        <span v-if="notification.read_at === null" class="unread-label">Unread</span>
      </div>
      <button
        type="button"
        class="text-button"
        :disabled="pending || !connected"
        @click="emit('read', notification, notification.read_at === null)"
      >
        Mark {{ notification.read_at === null ? "read" : "unread" }}
      </button>
    </div>
    <h2 :id="`summary-${notification.id}`">{{ notification.summary }}</h2>
    <MarkdownContent v-if="notification.message" :source="notification.message" :bridge="bridge" label="message" :initial-raw="rawMarkdown" />
    <button
      v-if="notification.details !== null"
      type="button"
      class="details-toggle"
      :aria-expanded="detailsOpen"
      @click="detailsOpen = !detailsOpen"
    >
      {{ detailsOpen ? "Hide details" : "Show details" }}
    </button>
    <MarkdownContent
      v-if="detailsOpen && notification.details !== null"
      :source="notification.details"
      :bridge="bridge"
      label="details"
      :initial-raw="rawMarkdown"
    />
    <div v-if="notification.tags.length" class="tags" aria-label="Tags">
      <span v-for="tag in notification.tags" :key="tag">{{ tag }}</span>
    </div>

    <section v-if="notification.response_state === 'pending'" class="response-controls" aria-label="Response options">
      <div class="response-buttons">
        <button
          v-for="option in notification.response_options"
          :key="option.id"
          type="button"
          :class="`response-${option.appearance}`"
          :disabled="pending || !connected"
          @click="choose(option)"
        >{{ option.label }}</button>
      </div>
      <form v-if="editing" @submit.prevent="submit">
        <label :for="`response-${notification.id}`">
          Message<span v-if="editing.message_mode === 'optional'"> (optional)</span>
        </label>
        <textarea
          :id="`response-${notification.id}`"
          ref="editor"
          v-model="responseMessage"
          maxlength="16385"
          :disabled="pending"
        />
        <div class="editor-actions">
          <button type="submit" :disabled="pending || !connected">Submit {{ editing.label }}</button>
          <button type="button" :disabled="pending" @click="editing = null">Cancel</button>
        </div>
      </form>
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
