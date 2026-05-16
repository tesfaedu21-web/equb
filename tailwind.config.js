module.exports = {
  content: ['./templates/**/*.html'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        equb: {
          green:  '#078930',
          yellow: '#FCDD09',
          red:    '#DA121A',
          dark:   '#030c06',
          mid:    '#081410',
          light:  '#143020',
          muted:  '#2d6a4f',
          pale:   '#e8f5ec',
          glow:   '#0ebd47',
        }
      },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        ethiopic: ['"Noto Sans Ethiopic"', 'sans-serif'],
      }
    }
  },
  plugins: [],
}
