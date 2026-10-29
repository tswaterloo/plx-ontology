"use client";

import { setNonce } from "get-nonce";

/**
 * Hands the request's CSP nonce to libraries that inject <style> elements at
 * runtime (Radix's scroll lock for menus and the mobile drawer, via
 * react-remove-scroll/get-nonce), so they pass the nonce-only style-src.
 */
export function StyleNonce({ nonce }: { nonce?: string }) {
  if (nonce) setNonce(nonce);
  return null;
}
