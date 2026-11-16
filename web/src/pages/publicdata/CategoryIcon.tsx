// Pictograms for the nearby-place chips.
//
// the reference app draws these with lucide-react. This app carries no icon
// dependency, so the same MAPPING is ported (category → which glyph) and the
// glyphs themselves are drawn here as small inline SVGs in the same visual
// language: 24-unit box, no fill, currentColor stroke, 2-unit round strokes.
// Categories with no entry fall back to a map pin, exactly as the reference app does.
type IconProps = { size?: number }

const S = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 2,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

function Svg({ size = 16, children }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      aria-hidden
      focusable="false"
      {...S}
    >
      {children}
    </svg>
  )
}

// Fuel — a pump with its nozzle arm.
const Fuel = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 21V5a2 2 0 0 1 2-2h5a2 2 0 0 1 2 2v16" />
    <path d="M3 21h12" />
    <path d="M6 9h5" />
    <path d="M16 8h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0v-6l-3-3" />
  </Svg>
)

// School — a roof over a hall.
const School = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3 2 8h20L12 3Z" />
    <path d="M4 8v13h16V8" />
    <path d="M10 21v-5h4v5" />
  </Svg>
)

// Building2 — an apartment block.
const Building2 = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 21V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v17" />
    <path d="M15 9h4a1 1 0 0 1 1 1v11" />
    <path d="M3 21h18" />
    <path d="M8 7h3M8 11h3M8 15h3" />
  </Svg>
)

// Utensils — fork and knife.
const Utensils = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 3v7a2 2 0 0 0 4 0V3" />
    <path d="M8 10v11" />
    <path d="M17 3c-1.5 1.5-2 3.5-2 6v3h4V3Z" />
    <path d="M17 12v9" />
  </Svg>
)

// BedDouble — a hotel bed.
const BedDouble = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 20v-9a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v9" />
    <path d="M3 15h18" />
    <path d="M7 9V7a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2" />
    <path d="M12 9V7a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v2" />
  </Svg>
)

// CircleParking — the P disc.
const CircleParking = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M10 17V7h3a3 3 0 0 1 0 6h-3" />
  </Svg>
)

// Bus — transit.
const Bus = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 17V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v11" />
    <path d="M4 11h16" />
    <path d="M3 17h18" />
    <circle cx="8" cy="19" r="1.5" />
    <circle cx="16" cy="19" r="1.5" />
  </Svg>
)

// Pill — pharmacy.
const Pill = (p: IconProps) => (
  <Svg {...p}>
    <rect x="2.5" y="8" width="19" height="8" rx="4" transform="rotate(-45 12 12)" />
    <path d="M8.5 8.5l7 7" />
  </Svg>
)

// Landmark — bank / ATM columns.
const Landmark = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 9 12 4l9 5" />
    <path d="M5 9v9M10 9v9M14 9v9M19 9v9" />
    <path d="M3 21h18" />
  </Svg>
)

// Coffee — a cup.
const Coffee = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 9h12v6a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4V9Z" />
    <path d="M16 10h2a2.5 2.5 0 0 1 0 5h-2" />
    <path d="M7 3v2M11 3v2" />
  </Svg>
)

// Store — an awning shopfront.
const Store = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 9 4.5 4h15L21 9" />
    <path d="M3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0" />
    <path d="M5 11v10h14V11" />
  </Svg>
)

// Zap — EV charging.
const Zap = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13 2 4 14h7l-1 8 9-12h-7l1-8Z" />
  </Svg>
)

// Hospital — a block with a cross.
const Hospital = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 21V5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v16" />
    <path d="M3 21h18" />
    <path d="M12 7v6M9 10h6" />
  </Svg>
)

// Trees — a park.
const Trees = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9 3 4 11h10L9 3Z" />
    <path d="M9 11v10" />
    <path d="M17 8l-3 6h6l-3-6Z" />
    <path d="M17 14v7" />
  </Svg>
)

// ShoppingCart — retail.
const ShoppingCart = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 4h2.5l2.5 11h10l2.5-8H6" />
    <circle cx="9" cy="19" r="1.5" />
    <circle cx="17" cy="19" r="1.5" />
  </Svg>
)

// MapPin — the fallback.
const MapPin = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 21s7-6.3 7-11a7 7 0 1 0-14 0c0 4.7 7 11 7 11Z" />
    <circle cx="12" cy="10" r="2.5" />
  </Svg>
)


// ── Beyond the reference app's set ────────────────────────────────────────────────
// the reference app maps 18 ids and lets everything else fall back to a map pin, which
// left 19 of this run's 32 categories sharing one glyph — the chip row and the
// "All" menu become unreadable at a glance. These fill that gap, keeping
// lucide's naming so the family stays consistent if an icon package ever
// arrives.

// Car — dealers.
const Car = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 13l2-5a2 2 0 0 1 2-1.4h10A2 2 0 0 1 19 8l2 5" />
    <path d="M3 13h18v5H3v-5Z" />
    <circle cx="7" cy="18" r="1.5" />
    <circle cx="17" cy="18" r="1.5" />
  </Svg>
)

// Wrench — auto parts.
const Wrench = (p: IconProps) => (
  <Svg {...p}>
    <path d="M15 3a5 5 0 0 0-4.3 7.5L3 18.2 5.8 21l7.7-7.7A5 5 0 1 0 15 3Z" />
  </Svg>
)

// Droplets — car wash.
const Droplets = (p: IconProps) => (
  <Svg {...p}>
    <path d="M8 3s3.5 4 3.5 6.5a3.5 3.5 0 0 1-7 0C4.5 7 8 3 8 3Z" />
    <path d="M16.5 10s3.5 4 3.5 6.5a3.5 3.5 0 0 1-7 0c0-2.5 3.5-6.5 3.5-6.5Z" />
  </Svg>
)

// Stethoscope — clinics.
const Stethoscope = (p: IconProps) => (
  <Svg {...p}>
    <path d="M5 3v5a4 4 0 0 0 8 0V3" />
    <path d="M9 12v3a5 5 0 0 0 10 0v-2" />
    <circle cx="19" cy="11" r="2" />
  </Svg>
)

// Baby — daycare.
const Baby = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="12" cy="8" r="5" />
    <path d="M10 7h.01M14 7h.01" />
    <path d="M9.5 10.5a3.5 3.5 0 0 0 5 0" />
    <path d="M6 21v-3a6 6 0 0 1 12 0v3" />
  </Svg>
)

// Tooth — dentists.
const Tooth = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 5c-2-1.5-6-2-7 1s0 6 .8 9.5C6.4 18 7 21 8.5 21s1.5-4 3.5-4 2 4 3.5 4 2.1-3 2.7-5.5C19 12 20 9 19 6s-5-2.5-7-1Z" />
  </Svg>
)

// Ticket — event venues.
const Ticket = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 8a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2 2 2 0 0 0 0 4 2 2 0 0 1-2 2H5a2 2 0 0 1-2-2 2 2 0 0 0 0-4Z" />
    <path d="M12 6v2M12 12v2" />
  </Svg>
)

// Sandwich — fast food.
const Sandwich = (p: IconProps) => (
  <Svg {...p}>
    <path d="M3 9a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4H3Z" />
    <path d="M3 12h18" />
    <path d="M4 15h16a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3Z" />
  </Svg>
)

// Flame — fire stations.
const Flame = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 21a6 6 0 0 0 6-6c0-4-3-5-4-9-2 1.5-3 3-3 5 0-1-1-2-2-3-1 2-3 4-3 7a6 6 0 0 0 6 6Z" />
  </Svg>
)

// Dumbbell — gyms.
const Dumbbell = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 9v6M7 7v10M17 7v10M20 9v6" />
    <path d="M7 12h10" />
  </Svg>
)

// Hammer — hardware.
const Hammer = (p: IconProps) => (
  <Svg {...p}>
    <path d="M14 3l7 7-3 3-7-7 3-3Z" />
    <path d="M11.5 8.5L3 17l4 4 8.5-8.5" />
  </Svg>
)

// Church — places of worship.
const Church = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 2v5M10 4h4" />
    <path d="M12 7 5 11v10h14V11l-7-4Z" />
    <path d="M10 21v-5h4v5" />
  </Svg>
)

// Shield — police.
const Shield = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3 5 6v6c0 4.5 3 7.5 7 9 4-1.5 7-4.5 7-9V6l-7-3Z" />
  </Svg>
)

// Mail — post offices.
const Mail = (p: IconProps) => (
  <Svg {...p}>
    <rect x="3" y="5" width="18" height="14" rx="2" />
    <path d="m3 7 9 6 9-6" />
  </Svg>
)

// Package — self storage.
const Package = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 3 3 7.5v9L12 21l9-4.5v-9L12 3Z" />
    <path d="M3 7.5 12 12l9-4.5M12 12v9" />
  </Svg>
)

// Trophy — sports facilities.
const Trophy = (p: IconProps) => (
  <Svg {...p}>
    <path d="M7 4h10v5a5 5 0 0 1-10 0V4Z" />
    <path d="M7 6H4v1a3 3 0 0 0 3 3M17 6h3v1a3 3 0 0 1-3 3" />
    <path d="M12 14v4M9 21h6" />
  </Svg>
)

// Truck — truck stops.
const Truck = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2 6h11v10H2V6Z" />
    <path d="M13 9h4l4 3.5V16h-8V9Z" />
    <circle cx="6.5" cy="18" r="1.5" />
    <circle cx="17" cy="18" r="1.5" />
  </Svg>
)

// GraduationCap — universities.
const GraduationCap = (p: IconProps) => (
  <Svg {...p}>
    <path d="M12 4 2 9l10 5 10-5-10-5Z" />
    <path d="M6 11.5V16c0 1.7 2.7 3 6 3s6-1.3 6-3v-4.5" />
  </Svg>
)

// PawPrint — veterinary.
const PawPrint = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="7" cy="8" r="1.8" />
    <circle cx="11" cy="5.5" r="1.8" />
    <circle cx="15.5" cy="7" r="1.8" />
    <circle cx="18" cy="11" r="1.8" />
    <path d="M9 14.5c-2 1.5-2.5 4.5 0 5.3 1.6.5 2-.8 3.5-.8s1.9 1.3 3.5.8c2.5-.8 2-3.8 0-5.3-1.5-1.1-2-2.5-3.5-2.5s-2 1.4-3.5 2.5Z" />
  </Svg>
)

// the reference app's CATEGORY_ICONS, id for id, plus the gap-fillers above.
const CATEGORY_ICONS: Record<string, (p: IconProps) => React.ReactElement> = {
  apartments: Building2,
  atm: Landmark,
  bank: Landmark,
  bus_stop: Bus,
  cafe: Coffee,
  convenience: Store,
  ev_charging: Zap,
  gas_station: Fuel,
  hospital: Hospital,
  hotel: BedDouble,
  park: Trees,
  parking: CircleParking,
  pharmacy: Pill,
  restaurant: Utensils,
  school: School,
  shopping_center: ShoppingCart,
  supermarket: ShoppingCart,
  train_station: Bus,
  // Gap-fillers: the reference app has no glyph for these, so they all collapsed to a
  // map pin. One category, one recognisable shape.
  auto_dealer: Car,
  auto_parts: Wrench,
  car_wash: Droplets,
  clinic: Stethoscope,
  daycare: Baby,
  dentist: Tooth,
  event_venue: Ticket,
  fast_food: Sandwich,
  fire_station: Flame,
  gym: Dumbbell,
  hardware: Hammer,
  place_of_worship: Church,
  police: Shield,
  post_office: Mail,
  self_storage: Package,
  sports_facility: Trophy,
  truck_stop: Truck,
  university: GraduationCap,
  veterinary: PawPrint,
}

export default function CategoryIcon({ category, size = 16 }: { category: string; size?: number }) {
  const Icon = CATEGORY_ICONS[category] ?? MapPin
  return <Icon size={size} />
}
