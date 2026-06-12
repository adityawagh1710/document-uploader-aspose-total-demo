import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        surface: {
          DEFAULT: '#0b1220', // page background (deep slate)
          raised: '#111a2c', // card background
          edge: '#1e293b', // borders / dividers
        },
        accent: {
          DEFAULT: '#22d3ee', // cyan — primary actions / live signals
          violet: '#a78bfa', // violet — secondary engine accent
        },
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
};

export default config;
