<script setup lang="ts">
import { onMounted, ref } from "vue";

import type { DesktopBridge } from "@/api/bridge";
import {
  MAX_CUSTOM_VIEWS,
  MAX_VIEW_REGEX_LENGTH,
  MAX_VIEW_RULES,
  type ClientSettings,
  type ViewRule,
} from "@/model/settings";

const props = defineProps<{ settings: ClientSettings; bridge: DesktopBridge }>();
const emit = defineEmits<{ close: []; saved: [settings: ClientSettings] }>();
function cloneSettings(settings: ClientSettings): ClientSettings {
  return {
    ...settings,
    views: settings.views.map((view) => ({
      ...view,
      rules: view.rules.map((rule) => ({ ...rule })),
    })),
  };
}

const draft = ref<ClientSettings>(cloneSettings(props.settings));
const saving = ref(false);
const error = ref<string | null>(null);
const closeButton = ref<HTMLButtonElement | null>(null);
const soundPathError = ref<string | null>(null);
let nextId = 1;

onMounted(() => closeButton.value?.focus());

function closeFromKeyboard(event: KeyboardEvent): void {
  if (event.key !== "Escape" || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey || saving.value) return;
  event.stopPropagation();
  event.preventDefault();
  emit("close");
}

function uniqueViewId(): string {
  let id: string;
  do id = `view-${Date.now()}-${nextId++}`;
  while (draft.value.views.some((view) => view.id === id));
  return id;
}

function addView(): void {
  if (draft.value.views.length >= MAX_CUSTOM_VIEWS) return;
  draft.value.views.push({ id: uniqueViewId(), name: "New view", rules: [{ domain_regex: ".*" }] });
}

function addRule(viewIndex: number): void {
  const rules = draft.value.views[viewIndex].rules;
  if (rules.length < MAX_VIEW_RULES) rules.push({ domain_regex: ".*" });
}

function removeRule(viewIndex: number, ruleIndex: number): void {
  const rules = draft.value.views[viewIndex].rules;
  if (rules.length > 1) rules.splice(ruleIndex, 1);
}

function move<T>(items: T[], index: number, offset: number): void {
  const target = index + offset;
  if (target < 0 || target >= items.length) return;
  [items[index], items[target]] = [items[target], items[index]];
}

function regexError(rule: ViewRule, key: keyof ViewRule): string | null {
  const value = rule[key];
  if (value === undefined || value === "") return null;
  if (value.length > MAX_VIEW_REGEX_LENGTH) return `Maximum length is ${MAX_VIEW_REGEX_LENGTH} characters.`;
  try {
    new RegExp(value, "i");
    return null;
  } catch {
    return "Invalid regular expression; this rule will be disabled when filtering.";
  }
}

function ruleIsValid(rule: ViewRule): boolean {
  const keys = ["domain_regex", "sender_regex", "tag_regex"] as (keyof ViewRule)[];
  return keys.some((key) => Boolean(rule[key])) && keys.every((key) => regexError(rule, key) === null);
}

function validRuleCount(rules: ViewRule[]): number {
  return rules.filter(ruleIsValid).length;
}

function cleanDraft(): ClientSettings {
  const result = cloneSettings(draft.value);
  for (const view of result.views) {
    for (const rule of view.rules) {
      for (const key of ["domain_regex", "sender_regex", "tag_regex"] as (keyof ViewRule)[]) {
        if (rule[key] === "") delete rule[key];
      }
    }
  }
  return result;
}

async function chooseSound(): Promise<void> {
  error.value = null;
  try {
    const path = await props.bridge.chooseSoundFile();
    if (path !== null) {
      draft.value.sound_path = path;
      await validateSoundPath();
    }
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "The file chooser failed.";
  }
}

async function validateSoundPath(): Promise<void> {
  soundPathError.value = null;
  if (!draft.value.sound_path) return;
  const result = await props.bridge.resolveSoundPath(draft.value.sound_path);
  if (result.ok) draft.value.sound_path = result.path;
  else soundPathError.value = result.error.message;
}

async function save(): Promise<void> {
  error.value = null;
  if (draft.value.views.some((view) => view.rules.some((rule) =>
    !rule.domain_regex && !rule.sender_regex && !rule.tag_regex))) {
    error.value = "Each rule must contain at least one expression.";
    return;
  }
  saving.value = true;
  try {
    const result = await props.bridge.updateSettings(cleanDraft());
    if (!result.ok) {
      error.value = result.error.message;
      return;
    }
    emit("saved", result.settings);
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : "Settings could not be saved.";
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="modal-backdrop">
    <section class="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title" @keydown="closeFromKeyboard">
      <header class="settings-heading">
        <h2 id="settings-title">Settings</h2>
        <button ref="closeButton" type="button" aria-label="Close settings" :disabled="saving" @click="emit('close')">×</button>
      </header>

      <form @submit.prevent="save">
        <fieldset>
          <legend>Presentation</legend>
          <label>Theme
            <select v-model="draft.theme">
              <option value="system">System</option><option value="light">Light</option><option value="dark">Dark</option>
            </select>
          </label>
          <label>Notification sound
            <select v-model="draft.sound">
              <option value="never">Never</option><option value="response_required">Responses required</option><option value="all">All notifications</option>
            </select>
          </label>
          <label>Custom sound file (optional)
            <span class="path-control"><input v-model="draft.sound_path" type="text" @blur="validateSoundPath"><button type="button" @click="chooseSound">Choose…</button></span>
          </label>
          <p v-if="soundPathError" class="inline-error" role="status">{{ soundPathError }}</p>
          <label class="checkbox-label"><input v-model="draft.hide_read" type="checkbox"> Hide read notifications</label>
          <label class="checkbox-label"><input v-model="draft.raw_markdown" type="checkbox"> Show raw Markdown by default</label>
        </fieldset>

        <section class="views-editor" aria-labelledby="views-title">
          <div class="section-heading"><h3 id="views-title">Custom views</h3><button type="button" :disabled="draft.views.length >= MAX_CUSTOM_VIEWS" @click="addView">Add view</button></div>
          <article v-for="(view, viewIndex) in draft.views" :key="view.id" class="view-editor">
            <div class="view-heading">
              <label>View name <input v-model="view.name" maxlength="80" required></label>
              <div class="reorder-buttons">
                <button type="button" :disabled="viewIndex === 0" :aria-label="`Move ${view.name} up`" @click="move(draft.views, viewIndex, -1)">↑</button>
                <button type="button" :disabled="viewIndex === draft.views.length - 1" :aria-label="`Move ${view.name} down`" @click="move(draft.views, viewIndex, 1)">↓</button>
                <button type="button" :aria-label="`Remove ${view.name}`" @click="draft.views.splice(viewIndex, 1)">Remove</button>
              </div>
            </div>
            <p v-if="validRuleCount(view.rules) === 0" class="inline-error" role="status">This view has no valid rules and will not be available for filtering.</p>
            <p v-else-if="validRuleCount(view.rules) < view.rules.length" class="inline-error" role="status">{{ view.rules.length - validRuleCount(view.rules) }} invalid rule(s) will be omitted from filtering.</p>
            <fieldset v-for="(rule, ruleIndex) in view.rules" :key="ruleIndex" class="rule-editor">
              <legend>Rule {{ ruleIndex + 1 }}</legend>
              <label>Domain expression <input v-model="rule.domain_regex" :maxlength="MAX_VIEW_REGEX_LENGTH + 1"></label>
              <p v-if="regexError(rule, 'domain_regex')" class="inline-error">{{ regexError(rule, 'domain_regex') }}</p>
              <label>Sender expression <input v-model="rule.sender_regex" :maxlength="MAX_VIEW_REGEX_LENGTH + 1"></label>
              <p v-if="regexError(rule, 'sender_regex')" class="inline-error">{{ regexError(rule, 'sender_regex') }}</p>
              <label>Tag expression <input v-model="rule.tag_regex" :maxlength="MAX_VIEW_REGEX_LENGTH + 1"></label>
              <p v-if="regexError(rule, 'tag_regex')" class="inline-error">{{ regexError(rule, 'tag_regex') }}</p>
              <div class="reorder-buttons">
                <button type="button" :disabled="ruleIndex === 0" :aria-label="`Move rule ${ruleIndex + 1} up`" @click="move(view.rules, ruleIndex, -1)">↑</button>
                <button type="button" :disabled="ruleIndex === view.rules.length - 1" :aria-label="`Move rule ${ruleIndex + 1} down`" @click="move(view.rules, ruleIndex, 1)">↓</button>
                <button type="button" :disabled="view.rules.length === 1" @click="removeRule(viewIndex, ruleIndex)">Remove rule</button>
              </div>
            </fieldset>
            <button type="button" :disabled="view.rules.length >= MAX_VIEW_RULES" @click="addRule(viewIndex)">Add rule</button>
          </article>
          <p v-if="!draft.views.length" class="empty">No custom views</p>
        </section>

        <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
        <div class="dialog-actions">
          <button type="button" :disabled="saving" @click="emit('close')">Cancel</button>
          <button type="submit" :disabled="saving">{{ saving ? "Saving…" : "Save settings" }}</button>
        </div>
      </form>
    </section>
  </div>
</template>
