// /app.js redirects here, and './menu.js' is /v2/menu.js only when the browser loaded this module from where the
// redirect ended.
import { addMenu } from './menu.js';

addMenu();
