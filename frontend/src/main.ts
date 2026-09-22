import { createApp } from "vue";

import App from "./App.vue";
import "./styles/base.css";
import { primeTheme } from "./presentation/theme";

primeTheme();
createApp(App).mount("#app");
