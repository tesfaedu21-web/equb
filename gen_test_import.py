import openpyxl
import random

# Ethiopian names pool
first_names = [
    "አበበ","ከበደ","ገረማ","ተስፋዬ","ዮሐንስ","ሙሉጌታ","ዳዊት","ሰለሞን","ብርሃኑ","ፍቅሩ",
    "ዘሪሁን","ሃይሌ","አስፋው","ታደሰ","ጌታቸው","ሞገስ","አለሙ","ዮናስ","ሳሙኤል","ዘካርያስ",
    "ናሁሰናይ","ሰናይ","ዓለሙ","ምህረቱ","ቤዛ","ሕይወት","ሰብለ","ሙሉነሽ","ፍቅርተ","ብርቅነሽ",
    "አዳነች","ሰናይት","ምስጋናው","ጽጌረዳ","ትዕግስት","አልማዝ","ፍርዕ","ናፍቆት","ሮቤ","ዝናሽ",
    "ሙሉ","ታሪኩ","ምህረት","ቃልኪዳን","ተወልደ","ዓዲሱ","ደሳለኝ","ሚናስ","ዓንዷለም","ልዑል",
    "ሰሜን","ኤልሳ","ኮኮብ","ዘላለም","ያሬድ","ዮሴፍ","ሱራፌ","ሸዋ","ፍቅርሰን","ቴስፋ",
    "ምዕራፍ","ሰምሃት","ሩት","ሚርያም","ሃና","ሳራ","ሊዲያ","ዲና","ሶፊያ","ሄለን",
    "ሚካኤል","ርዕዮት","ፋሲካ","ወርቅነሽ","አሚናታ","ጸሃይ","ምሕረት","ሰናበት","ዘሩ","ላሊ",
]

last_names = [
    "ተክሉ","ወርቁ","ሙሉ","ሃይሌ","ደምሴ","ጋሻው","አዲሱ","ነጋሽ","ታዬ","አበጋዝ",
    "ዘርጋው","ሻምቦ","ዳምጠ","ዘረዙ","ቃሉ","ሆሳዕ","ወልደ","ሙሉጌታ","ሃይማኖት","ደርሶ",
    "ዮሴፍ","ብርሃኔ","ሊቀ","ተሰማ","ዓለሙ","ፈቴነ","ሞሃ","ጂማ","ናኦ","ሌሊሳ",
    "ከበደ","ወ/ሚካኤል","ወ/ጊዮርጊስ","ሀ/ማሪያም","ሀ/ሚካኤል","ሀ/ስላሴ","ሀ/ጊዮርጊስ",
    "አያሌው","ጉዲሳ","ጌቱ","ሶሪ","ቡሬ","ናጋሶ","ደሳ","ቶላ","ቢቂሳ","ወዳጆ",
]

def random_name(used):
    for _ in range(100):
        name = f"{random.choice(first_names)} {random.choice(last_names)}"
        if name not in used:
            used.add(name)
            return name
    return f"አባል {len(used)+1}"

def random_phone():
    return f"+2519{random.randint(10000000,99999999)}"

random.seed(42)
used_names = set()
rows = []  # (name, phone, spot_number, share)

# Randomly decide which spots are full (1 person) vs half (2 people)
# ~70 full spots, ~46 half spots (46*2=92 people) → total ~162 people across 116 spots
# Let's do: 70 full + 46 half = 116 spots, 70 + 92 = 162 members
full_spots  = sorted(random.sample(range(1, 117), 70))
half_spots  = [s for s in range(1, 117) if s not in full_spots]  # 46 spots

for s in full_spots:
    rows.append((random_name(used_names), random_phone(), s, "full"))

for s in half_spots:
    rows.append((random_name(used_names), random_phone(), s, "half"))
    rows.append((random_name(used_names), random_phone(), s, "half"))

# Sort by spot number
rows.sort(key=lambda r: r[2])

# Write Excel
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Members"
ws.append(["Name", "Phone", "Spot Number", "Share", "Notes"])
for r in rows:
    ws.append(list(r) + [""])

out = "test_import_116spots.xlsx"
wb.save(out)

full_count  = sum(1 for r in rows if r[3] == "full")
half_count  = sum(1 for r in rows if r[3] == "half")
print(f"Created {out}")
print(f"Total members : {len(rows)}")
print(f"Full spots    : {len(full_spots)} spots × 1 = {full_count} members")
print(f"Half spots    : {len(half_spots)} spots × 2 = {half_count} members")
print(f"Total spots   : {len(full_spots)+len(half_spots)} (all 116 covered)")
