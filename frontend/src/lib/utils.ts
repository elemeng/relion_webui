// SPDX-FileCopyrightText: 2026 elemeng
// SPDX-License-Identifier: GPL-2.0-or-later

import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Merge conditional class names, letting later Tailwind classes win. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
