interface Props {
  className?: string;
}

export default function AssistantIcon({ className }: Props) {
  return (
    <svg className={className} viewBox="0 0 32 32" width="32" height="32" role="img" aria-label="助手" focusable="false">
      <path d="M16 3v3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <circle cx="16" cy="2.5" r="1.5" fill="currentColor" />
      <rect x="5" y="7" width="22" height="19" rx="7" fill="currentColor" opacity="0.16" />
      <rect x="6.5" y="8.5" width="19" height="16" rx="5.5" fill="none" stroke="currentColor" strokeWidth="2" />
      <circle cx="12" cy="16" r="2" fill="currentColor" />
      <circle cx="20" cy="16" r="2" fill="currentColor" />
      <path d="M11 21c1.5 1.4 3.1 2 5 2s3.5-.6 5-2" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
