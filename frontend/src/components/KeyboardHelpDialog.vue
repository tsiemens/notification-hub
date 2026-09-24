<script setup lang="ts">
import { onMounted, ref } from "vue";

const emit = defineEmits<{ close: [] }>();
const closeButton = ref<HTMLButtonElement | null>(null);

onMounted(() => closeButton.value?.focus());

function handleKeydown(event: KeyboardEvent): void {
  if (event.ctrlKey || event.altKey || event.metaKey) return;
  if (event.key === "Escape" && !event.shiftKey) {
    event.stopPropagation();
    event.preventDefault();
    emit("close");
  } else if (event.key === "Tab") {
    event.preventDefault();
    closeButton.value?.focus();
  }
}
</script>

<template>
  <div class="modal-backdrop">
    <section
      class="keyboard-help-dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="keyboard-help-title"
      @keydown="handleKeydown"
    >
      <header class="settings-heading">
        <h2 id="keyboard-help-title">Keyboard shortcuts</h2>
        <button ref="closeButton" type="button" aria-label="Close keyboard shortcuts" @click="emit('close')">×</button>
      </header>
      <table>
        <thead><tr><th scope="col">Key</th><th scope="col">Action</th></tr></thead>
        <tbody>
          <tr><th scope="row"><kbd>j</kbd> / <kbd>k</kbd></th><td>Select next / previous visible notification</td></tr>
          <tr><th scope="row"><kbd>d</kbd></th><td>Toggle details on the selected notification</td></tr>
          <tr><th scope="row"><kbd>m</kbd></th><td>Toggle read / unread on the selected notification</td></tr>
          <tr><th scope="row"><kbd>Home</kbd></th><td>Select and reveal the newest visible notification</td></tr>
          <tr><th scope="row"><kbd>?</kbd></th><td>Open this keyboard help</td></tr>
          <tr><th scope="row"><kbd>Escape</kbd></th><td>Close help or settings, or cancel a response editor</td></tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
