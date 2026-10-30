import { useState } from "react";

import type { Menu, MenuProduct } from "../api/types";
import { Dialog, Stepper } from "./controls";
import { formatCents, formatMoney } from "./money";
import { lineTotalCents, selectionProblems, unavailableLabel } from "./order";

export interface ProductChoice {
  quantity: number;
  modifierIds: string[];
  notes: string | null;
}

interface MenuPanelProps {
  menu: Menu;
  // Products being added right now.
  busyProductIds: ReadonlySet<string>;
  disabled: boolean;
  onAdd(product: MenuProduct, choice: ProductChoice): void;
}

export function MenuPanel({ menu, busyProductIds, disabled, onAdd }: MenuPanelProps) {
  const [categoryId, setCategoryId] = useState<string | null>(menu.categories[0]?.id ?? null);
  const [configuring, setConfiguring] = useState<MenuProduct | null>(null);
  const category = menu.categories.find((candidate) => candidate.id === categoryId) ?? menu.categories[0];

  if (!category) {
    return (
      <section className="menu state" aria-label="Menu">
        <h2 className="state__title">The menu is empty</h2>
        <p className="state__text">No active categories. A manager has to set up the menu first.</p>
      </section>
    );
  }

  function choose(product: MenuProduct) {
    // Products without options are added straight away; quantity and a
    // note can still be changed in the order before sending.
    if (product.modifier_groups.length === 0) {
      onAdd(product, { quantity: 1, modifierIds: [], notes: null });
    } else {
      setConfiguring(product);
    }
  }

  return (
    <section className="menu" aria-label="Menu">
      <div className="menu__categories" role="tablist" aria-label="Categories">
        {menu.categories.map((candidate) => (
          <button
            key={candidate.id}
            type="button"
            role="tab"
            aria-selected={candidate.id === category.id}
            className={`chip${candidate.id === category.id ? " chip--current" : ""}`}
            onClick={() => setCategoryId(candidate.id)}
          >
            {candidate.name}
          </button>
        ))}
      </div>

      {category.products.length === 0 ? (
        <p className="column__empty">No products in {category.name}.</p>
      ) : (
        <ul className="menu__products" aria-label={category.name}>
          {category.products.map((product) => {
            const busy = busyProductIds.has(product.id);
            return (
              <li key={product.id}>
                <button
                  type="button"
                  className={`product${product.is_available ? "" : " product--unavailable"}`}
                  disabled={disabled || !product.is_available || busy}
                  onClick={() => choose(product)}
                  aria-label={`${product.name}, ${formatMoney(product.price, menu.currency)}${
                    product.is_available ? "" : `, ${unavailableLabel(product)}`
                  }`}
                >
                  <span className="product__name">{product.name}</span>
                  <span className="product__price">{formatMoney(product.price, menu.currency)}</span>
                  <span className="product__meta">
                    {product.is_available
                      ? busy
                        ? "Adding…"
                        : (product.station?.name ?? "")
                      : unavailableLabel(product)}
                    {product.is_available && product.modifier_groups.length > 0 && !busy && " · options"}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {configuring && (
        <ProductDialog
          product={configuring}
          currency={menu.currency}
          onCancel={() => setConfiguring(null)}
          onAdd={(choice) => {
            setConfiguring(null);
            onAdd(configuring, choice);
          }}
        />
      )}
    </section>
  );
}

interface ProductDialogProps {
  product: MenuProduct;
  currency: string;
  onCancel(): void;
  onAdd(choice: ProductChoice): void;
}

export function ProductDialog({ product, currency, onCancel, onAdd }: ProductDialogProps) {
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [quantity, setQuantity] = useState(1);
  const [notes, setNotes] = useState("");
  const [tried, setTried] = useState(false);
  const problems = selectionProblems(product.modifier_groups, selected);

  function toggle(groupId: string, modifierId: string) {
    const group = product.modifier_groups.find((candidate) => candidate.id === groupId)!;
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(modifierId)) {
        next.delete(modifierId);
      } else if (group.max_selections === 1) {
        // A single choice: picking another option replaces it.
        for (const modifier of group.modifiers) next.delete(modifier.id);
        next.add(modifierId);
      } else {
        next.add(modifierId);
      }
      return next;
    });
  }

  function submit() {
    setTried(true);
    if (problems.size > 0) return;
    onAdd({ quantity, modifierIds: [...selected], notes: notes.trim() || null });
  }

  return (
    <Dialog title={product.name} onClose={onCancel}>
      {product.description && <p className="dialog__text">{product.description}</p>}

      {product.modifier_groups.map((group) => {
        const problem = problems.get(group.id);
        const full =
          group.max_selections !== null &&
          group.max_selections > 1 &&
          group.modifiers.filter((modifier) => selected.has(modifier.id)).length >= group.max_selections;
        return (
          <fieldset key={group.id} className="options">
            <legend className="options__title">
              {group.name}
              <span className="options__rule">{ruleText(group.min_selections, group.max_selections, group.is_required)}</span>
            </legend>
            <div className="options__list">
              {group.modifiers.map((modifier) => {
                const checked = selected.has(modifier.id);
                return (
                  <button
                    key={modifier.id}
                    type="button"
                    role={group.max_selections === 1 ? "radio" : "checkbox"}
                    aria-checked={checked}
                    className={`chip chip--option${checked ? " chip--current" : ""}`}
                    disabled={!checked && full}
                    onClick={() => toggle(group.id, modifier.id)}
                  >
                    {modifier.name}
                    {modifier.price_adjustment !== "0.00" && (
                      <span className="chip__price"> +{formatMoney(modifier.price_adjustment, currency)}</span>
                    )}
                  </button>
                );
              })}
              {group.modifiers.length === 0 && <span className="options__rule">No options available</span>}
            </div>
            {tried && problem && (
              <p className="dialog__error" role="alert">
                {group.name}: {problem}
              </p>
            )}
          </fieldset>
        );
      })}

      <div className="dialog__row">
        <span className="field__label">Quantity</span>
        <Stepper label="Quantity" value={quantity} min={1} max={99} onChange={setQuantity} />
      </div>

      <label className="field">
        <span className="field__label">Note for the kitchen/bar</span>
        <textarea
          className="field__input field__textarea"
          value={notes}
          maxLength={500}
          rows={2}
          placeholder="e.g. no onions, allergy: nuts"
          onChange={(event) => setNotes(event.target.value)}
        />
      </label>

      <div className="dialog__actions">
        <button type="button" className="button button--secondary" onClick={onCancel}>
          Cancel
        </button>
        <button type="button" className="button button--primary" onClick={submit}>
          Add · {formatCents(lineTotalCents(product, selected, quantity), currency)}
        </button>
      </div>
    </Dialog>
  );
}

function ruleText(min: number, max: number | null, required: boolean): string {
  const least = required ? Math.max(min, 1) : min;
  if (max === 1) return least >= 1 ? "Required · choose one" : "Optional · up to one";
  if (least > 0 && max !== null) return `Choose ${least}–${max}`;
  if (least > 0) return `Choose at least ${least}`;
  if (max !== null) return `Optional · up to ${max}`;
  return "Optional";
}
