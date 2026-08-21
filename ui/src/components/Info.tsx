/**
 * Erklaerungs-Tooltip.
 *
 * Spec 23: keine Fachbegriffe ohne Erklaerung. Fachbegriffe bleiben in ihrer
 * ueblichen englischen Form stehen - erklaert wird beim Darauffahren.
 */

interface Props {
  text: string;
}

export function Info({ text }: Props) {
  return (
    <span className="info" tabIndex={0} role="note" aria-label={text} title={text}>
      ?
    </span>
  );
}

interface TermProps {
  label: string;
  explanation: string;
}

export function Term({ label, explanation }: TermProps) {
  return (
    <span className="term">
      {label}
      <Info text={explanation} />
    </span>
  );
}
