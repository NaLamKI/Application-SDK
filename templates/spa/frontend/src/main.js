// extFetch adds the CSRF header and, on 401, sends the whole page to the sign-in
// and back (a top-level navigation – the app can only intercept that).
import { extFetch } from "/_sdk/client.js";

const root = document.getElementById("app");

function node(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text; // textContent, never innerHTML: the data is not ours
  if (className) element.className = className;
  return element;
}

async function getJson(path) {
  const response = await extFetch(path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

function render(me, fields) {
  root.replaceChildren(
    node("h1", document.title),
    node("p", `Signed in as ${me.name || me.email || me.sub}`, "muted"),
    node("h2", "Your fields"),
  );
  if (fields.length === 0) {
    root.append(node("p", "No fields yet.", "muted"));
    return;
  }
  const list = document.createElement("ul");
  for (const field of fields) {
    const area = field.area == null ? "" : ` – ${field.area} ${field.areaUnit || ""}`.trimEnd();
    list.append(node("li", (field.name || field.id) + area));
  }
  root.append(list);
}

async function main() {
  try {
    const [me, fields] = await Promise.all([getJson("/api/me"), getJson("/api/fields")]);
    render(me, fields);
  } catch (error) {
    root.replaceChildren(node("p", `Something went wrong: ${error.message}`, "error"));
  }
}

main();
