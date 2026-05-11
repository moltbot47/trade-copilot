/**
 * Twitter card image — same canvas as OG, exported separately so Twitter's
 * scraper picks it up explicitly. Twitter prefers a wider crop (1200x675
 * for "summary_large_image") so we render at 1200x600 which fits both.
 */
export { default, alt, size, contentType } from "./opengraph-image";
