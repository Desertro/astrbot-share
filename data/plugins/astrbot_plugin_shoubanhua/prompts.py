# 手办化插件提示词配置文件
# 可以在这里增加、删除或修改提示词内容和描述

prompt_map = {
    "手办化帮助": {"prompt": "获取手办化插件的基本使用帮助", "description": "获取手办化插件的基本使用帮助"},

    "手办化命令帮助": {"prompt": "获取手办化插件的所有命令列表和简介", "description": "获取手办化插件的所有命令列表和简介"},

    "手办化查询次数": {"prompt": "查询个人和群组剩余使用次数", "description": "查询用户的个人剩余次数和群组共享剩余次数"},

    "手办化签到": {"prompt": "每日签到获取使用次数", "description": "每日签到获取额外的使用次数"},

    "手办化添加key": {"prompt": "管理员添加API密钥", "description": "管理员添加用于调用API的密钥"},

    "手办化key列表": {"prompt": "管理员查看API密钥列表", "description": "管理员查看当前配置的API密钥列表"},

    "手办化删除key": {"prompt": "管理员删除API密钥", "description": "管理员删除指定的API密钥"},

    "手办化增加用户次数": {"prompt": "管理员增加用户使用次数", "description": "管理员为指定用户增加使用次数"},

    "手办化增加群组次数": {"prompt": "管理员增加群组使用次数", "description": "管理员为指定群组增加使用次数"},

    "bnn": {"prompt": "用户自定义提示词", "description": "默认走 Gemini 系列模型；无图文生图，有图图生图"},
    "bna": {"prompt": "无图自定义提示词", "description": "无需图片，直接使用自定义提示词生成图像"},
    "gpt": {"prompt": "用户自定义 GPT 图像提示词", "description": "默认走 GPT-Image 2.5；无图文生图，有图图生图"},

    "手办化1": {"prompt": "Your task is to create a photorealistic, masterpiece-quality image of a 1/7 scale commercialized figurine based on the user's character. The final image must be in a realistic style and environment.\n\n**Crucial Instruction on Face & Likeness:** The figurine's face is the most critical element. It must be a perfect, high-fidelity 3D translation of the character from the source image. The sculpt must be sharp, clean, and intricately detailed, accurately capturing the original artwork's facial structure, eye style, expression, and hair. The final result must be immediately recognizable as the same character, elevated to a premium physical product standard. Do NOT generate a generic or abstract face.\n\n**Scene Composition (Strictly follow these details):**\n1. **Figurine & Base:** Place the figure on a computer desk. It must stand on a simple, circular, transparent acrylic base WITHOUT any text or markings.\n2. **Computer Monitor:** In the background, a computer monitor must display 3D modeling software (like ZBrush or Blender) with the digital sculpt of the very same figurine visible on the screen.\n3. **Artwork Display:** Next to the computer screen, include a transparent acrylic board with a wooden base. This board holds a print of the original 2D artwork that the figurine is based on.\n4. **Environment:** The overall setting is a desk, with elements like a keyboard to enhance realism. The lighting should be natural and well-lit, as if in a room.", "description": "画风偏写实，适合很二次元的图片"},

    "手办化2": {"prompt": "Use the nano-banana model to create a 1/7 scale commercialized figure of thecharacter in the illustration, in a realistic styie and environment.Place the figure on a computer desk, using a circular transparent acrylic basewithout any text.On the computer screen, display the ZBrush modeling process of the figure.Next to the computer screen, place a BANDAl-style toy packaging box printedwith the original artwork.", "description": "画风偏二次元，容易变成纸片人，推荐写实照片"},

    "手办化3": {"prompt": "Realistic PVC figure based on the game screenshot character, exact pose replication highly detailed textures PVC material with subtle sheen and smooth paint finish, placed on an indoor wooden computer desk (with subtle desk items like a figure box/mouse), illuminated by soft indoor light (mix of desk lamp and natural window light) for realistic shadows and highlights, macro photography style,high resolution,sharp focus on the figure,shallow depth of field (desk background slightly blurred but visible), no stylization,true-to-reference color and design, 1:1scale.", "description": "画风偏二次元，容易变成纸片人，推荐写实照片，无电脑桌"},

    "手办化4": {"prompt": "Create a premium, collectible 1/7 scale standalone figurine based on the image, meticulously replicating the character, made from smooth PVC and ABS plastic with a professional matte finish. It stands on a minimalist transparent acrylic base. Next to it is its retail packaging box displaying the price and brand information, with the figure wrapped in plastic inside the slightly larger box. They are naturally arranged on a clean wooden table surrounded by reference books, with a bookshelf in the background and soft afternoon sunlight streaming through the window. Photo-realistic, DSLR effect, depth of field, bokeh background.", "description": "没有电脑桌"},

    "贴纸化": {"prompt": "Generate A creative collage artwork based on the provided input image. The artwork should be created using a variety of materials such as paper, fabric, and found objects to achieve a textured, layered look. The composition should capture the essence of the original subject while incorporating collage techniques such as cutting, layering, and mixed media. The final piece should have a dynamic", "description": "将图像转换为卡通贴纸风格"},

    "棉花娃娃化": {"prompt": "Reshape the character in the picture into a top-tier collectible *fumo*, with a fully soft and dynamic pose, and place it on the character theme fur pad. High-precision material, hand-stitched, the texture of the plush fabric and the clothing is truly distinct.\nIts eyes are the signature large embroidered semi-oval ones, without pupils, presenting a flat, sleepy or listless expression.\nThe main light source is soft diffused light, highlighting the fluffy feeling and soft texture, without overexposure. Powerful fill light eliminates dead black, and details are fully visible. The background is a blurred depth of field by the window, and the product packaging box is faintly visible on the side and rear. The sticker on the packaging box should be the original uploaded image.\nMuseum-level photography quality, every detail of the body is intact, and the embroidered facial features are exquisite and accurate.\nProhibited: Any 2D elements or direct copying of the original image, plastic feel, hard texture, blurred face, misaligned facial features, and loss of details.", "description": "将图像转换为棉花娃娃玩偶风格"},

    "服装分解": {"prompt": "A professional e-commerce fashion showcase featuring the clothing worn by the character from the input image, presented in a clean, studio-style setting. The outfit is decomposed into individual pieces (e.g., top, bottom, jacket, shoes, accessories), each clearly displayed and arranged in an organized, visually appealing layout. Each clothing item retains the exact design, color, texture, and details from the original image, showcased with crisp lighting and high-definition clarity. The background is minimalistic, white or neutral, to emphasize the clothing details, suitable for an online retail platform. The presentation includes subtle annotations or labels for each item, ensuring a polished, catalog-style look for wear and styling inspiration.", "description": "将图像中的人物服装分解为各个组成部分"},

    "模型套件": {"prompt": "Convert the people in the photos to the style of a model kit box, rendered in isometric perspective. Label the box with the title“Zhogue”. Inside the box, a gouda-styled robotic version of the person in the photo is displayed, along with its essentials (such as cosmetics, bags, or other items) redesigned as a futuristic mechanical accessory. The box should resemble a real Gunpla box, with technical illustrations, manual-style details, and sci-fi fonts. Next to the box, the actual gouda-style robot itself is also displayed, rendered in a realistic and lifelike style on the outside of the packaging, similar to the official Bandai propaganda renderings.", "description": "将图像转换为模型套件风格"},

    "角色界面": {"prompt": "Transform the input person image into a game character selection interface. Display the character on the right side as a full-body portrait, standing upright at a 45-degree angle facing both the screen and the left-side selection module, mimicking a selected state in a video game. Retain the person image's facial features, expression, and hairstyle, but adapt the clothing to a game-inspired style (e.g., fantasy armor, sci-fi suit, or RPG adventurer outfit) with intricate details, vibrant textures, and thematic accessories, while preserving the original clothing's color scheme and general aesthetic. If the input is a half-body image, seamlessly complete the lower body, matching the game-style clothing and proportions. On the left side, present a sleek interface with selectable options including game-style clothing variations (e.g., different armor sets, robes, tactical gear), martial stats (e.g., strength, agility), equipment (e.g., swords, gadgets, shields), and health points, styled as interactive game UI elements with clear labels and modern design. Arrange the layout to mimic a video game character selection screen, with a smooth, unified background gradient (e.g., dark blue to soft gray) for a cohesive, natural transition across the image. Use consistent, cinematic lighting and subtle glow effects to enhance the game-like atmosphere while maintaining the character's real-world facial essence.", "description": "将图像转换为游戏角色界面风格"},

    "角色设定": {"prompt": "为我生成人物的角色设定（Character Design）,比例设定（不同身高对比、头身比等）,三视图（正面、侧面、背面）,表情设定（Expression Sheet） → 就是你发的那种图,动作设定（Pose Sheet） → 各种常见姿势,服装设定（Costume Design）", "description": "将图像转换为角色设定表风格"},

    "钥匙扣": {"prompt": "Turn this photo into a cute charm / a flat acrylic keychain / a flat rubber keychain to hang on an LV bag / the bag in photo 2.", "description": "将图像转换为钥匙扣风格"},

    "姿势表": {"prompt": "请为这幅插图创建一个姿势表，摆出各种姿势", "description": "将图像中的角色转换为姿势表风格"},

    "转身动画": {"prompt": "show me this scene from behind the subjects. keep the details and the lighting identical", "description": "将图像中的人物转换为转身动画"},

    "多机位": {"prompt": "生成这张图片的正脸特写、侧身照、远景、背影的四种多机位镜头，然后整合输出到一张照片里，保持人物高度的一致性，适合生成连续剧情感镜头", "description": "将图像转换为多机位拍摄风格"},

    "电影分镜": {"prompt": "用这图里的角色创作一个令人上瘾的12部分故事，包含12张图像，讲述经典的黑色电影侦探故事。故事关于他们寻找线索并最终发现的失落的宝藏。整个故事充满刺激，有情感的高潮和低谷，以精彩的转折和高潮结尾。不要在图像中包含任何文字或文本，纯粹通过图像本身讲述故事", "description": "将图像转换为电影分镜风格"},

    "动漫分镜": {"prompt": "According to the content of the picture to generate nine frames of comics, with pictures and lenses to tell a story.", "description": "将图像转换为动漫分镜风格"},

    "真人化": {"prompt": "in Studio, pure white background, a cosplayer dressed in an identical outfit, as the girl in the reference image, mimicking her pose and outfit. enhanced with film grain for a gritty, authentic particle effect reminiscent of 35mm film stock; 8K ultra-HD, sharp and believable, no abstraction.", "description": "将图像中的动漫或游戏角色转换为真人风格"},

    "场景真人化": {"prompt": "Generate a highly detailed photo of a girl cosplaying this illustration, at Comiket. Exactly replicate the same pose, body posture, hand gestures, facial expression, and camera framing as in the original illustration. Keep the same angle, perspective, and composition, without any deviation", "description": "将图像中的动漫或游戏场景转换为真人电影风格"},

    "半真人半动漫": {"prompt": "Take the image of the woman in [Input Photo]. Apply a creative split-style effect. Keep the lower half of her body (legs and boots) as the original photograph. Transform the upper half of her body (torso, arms, head) into a vibrant, 2D anime style with bold outlines and flat colors, similar to the anime 'Cyberpunk: Edgerunners'. The transition between the two styles should be a clean, slightly curved line.", "description": "将图像转换为半真人半动漫风格"},

    "元素融合": {"prompt": "A striking, high-definition frontal portrait of the character from the input photo, with the face perfectly centered. The image blends two styles seamlessly: the left half retains the character's original realistic appearance, including detailed skin textures, natural lighting, and exact facial features from the input image, while the right half transitions into a vibrant manga-style version, featuring bold outlines, expressive eyes, and stylized features typical of high-quality anime art, while preserving recognizable traits (e.g., hair shape, facial structure). The transition between the realistic and manga halves is smooth and gradual, with no visible dividing line, ensuring a natural, cohesive fusion at the center of the face. The blending emphasizes the contrast between realistic and manga aesthetics while maintaining a unified appearance. The background is a neutral, solid color (e.g., soft gray or white) to highlight the fusion effect, with consistent lighting to enhance the artistic impact and no harsh separation.", "description": "将图像中的两个或多个元素融合在一起"},

    "猫娘化": {"prompt": "Transform the input character into a catgirl version with fluffy cat ears, tail, while keeping the original face, hairstyle, outfit, and chibi proportions in nano-banana anime style.", "description": "将图像中的人物转换为猫娘风格"},

    "coser化1": {"prompt": "A scene in a bright, modern restaurant at night, created to replicate the original image provided. On the table, there are foods like hamburgers and French fries, a crumpled used tissue, a few food crumbs (adding a sense of realism), and an Android phone (with an anime character displayed on the screen) lying on the surface—you (the viewer) are holding a hamburger or French fries in your hand. At a very nearby separate table (not a shared table)—so close that it's within easy sight—two Chinese people (appearing as a couple)—one being a coser dressed exactly as the anime character on your Android phone, and the other a man—are sitting and engaging in intimate couple interactions (e.g., gentle eye contact, leaning slightly towards each other, or one resting a hand lightly on the other's arm). On their table, there is a two-person set meal, and both figures are slightly blurred (not overly so). The overall atmosphere blends a relaxed dining vibe with anime elements, featuring natural lighting, consistent with the visual style of the original image.", "description": "第三视角麦当劳桌上coser化"},

    "coser化2": {"prompt": "Generate a highly detailed, realistic photo: At a Comiket venue, a real girl is sitting directly opposite you, wearing an exquisite anime character cosplay costume. On your table is a double meal set with hamburgers and French fries, alongside crumpled tissues and some food scraps, creating a sense of realism. Your Android phone is casually placed on the table, its screen displaying an unmodified image of the original character. She is engaging in intimate interaction with you, gazing gently into your eyes, her body slightly leaning towards you, and one hand gently placed on your arm. You are holding a hamburger or French fries. The overall image blends a relaxed dining atmosphere with anime elements, using natural light, in a bright and modern style.", "description": "第二视角麦当劳桌上coser化"},

    "coser化3": {"prompt": "A highly detailed DSLR photo of a real human girl cosplaying the reference illustration at Comiket, exactly replicating the same pose, facial expression, and outfit, with natural human face, realistic skin texture, professional cosplay photography, one leg wearing a black thigh-high stocking, the other leg wearing a short white sock with a bow, asymmetric clothing details, authentic real-life cosplay photo", "description": "原场景原动作coser化"},

    "coser化4": {"prompt": "Convert the illustrated character into a real person, with the appearance of a Japanese cosplay girl. The face should be narrow with a slightly pointed chin, smooth facial lines, delicate and well-defined features, large expressive eyes slightly upturned at the corners, a slim nose, and small lips. Keep the hairstyle, outfit, and pose highly faithful to the original illustration. The photo should look like a casual iPhone selfie, unposed and natural, slightly blurred due to movement, evenly illuminated by indoor venue lighting. Aspect ratio 9:16. The final image must look like a real candid photo, not AI-generated.", "description": "精心调配"},

    "痛房化": {"prompt": "Transform the uploaded indoor photo into a Japanese-style ita-room with the following specific requirements: Walls: Generate multi-size posters/scrolls (A2/A3/banner mixed arrangement) in an orderly matrix; no watermarks or garbled text. Curtains and bedding: Fully replace with themed patterns while retaining fabric folds and textures; pillowcases and life-sized cushions use the same character design. Display: Add glass display cabinets and open shelves, densely displaying themed figurines, acrylic stands, badge boards, and boxed peripherals of the same theme; arrange them in groups by height and color system. Desk: Keep the original equipment and light and shadow, only replace the screensaver/wallpaper with themed images; organize the wires neatly. Lighting: Add soft RGB light strips (along the ceiling and desk edges), coordinated with the main color, avoiding overexposure and color overflow. Texture: Realistic materials for PVC figurines, spray-painted paper, acrylic, and cotton fabrics; natural glass reflections without ghosting. Consistency: The face, hair color, and clothing details of the character on all carriers (posters/cushions/stands/box art) must maintain the same character and art style. Constraints (negative): No brand logos, watermarks, typos, distorted faces, perspective errors, repeated textures, over-sharpening, or noise.",  "description": "温馨的二次元小房间"},

    "痛车化": {"prompt": "A realistic photo of a modern sports car wrapped with an anime itasha design.The side of the car features a large, high-resolution decal of the reference illustration, covering the car doors and extending to the fenders.Glossy vinyl wrap, sharp colors, highly detailed itasha style, otaku car culture, Japanese style anime decal, professional automotive photography, indoor parking garage.", "description": "二次元痛车"},

    "地雷女化": {"prompt": "Replace the outfit with a Japanese jirai-kei (地雷系) style outfit: a pastel pink frilly blouse with puffed sleeves, a large black ribbon bow at the chest, a short black pleated skirt with a shiny texture, a black belt decorated with silver heart-shaped buckles, and black thigh-high stockings, while keeping the person’s original face, hairstyle, pose, and body proportions unchanged", "description": "把角色衣服换成地雷女搭配"},

    "女仆化": {"prompt": "Replace the outfit with a stylish maid uniform: a black long-sleeved dress with puffed shoulders, a white frilly apron tied at the waist, a turquoise ribbon bow at the chest, white gloves, white stockings, and black-and-white shoes, while keeping the person’s original face, hairstyle, pose, and body proportions unchanged", "description": "把角色衣服换成女仆搭配"},

    "JK化": {"prompt": "Replace the outfit with a Japanese JK school uniform: a black blazer with white trim and silver buttons, a white collared shirt, a red ribbon bow tie at the chest, a pleated skirt with black, red, and gray plaid pattern, paired with long black thigh-high socks and black loafers, while keeping the person’s original face, hairstyle, pose, and body proportions unchanged", "description": "把角色衣服换成JK学校搭配"},

    "兔女郎化": {"prompt": "Replace the outfit with a bunny-themed stage costume: a glossy black one-piece bodysuit with a satin finish, paired with black patterned tights, black high-heeled shoes, white wrist cuffs, a turquoise bow tie at the neck, and a black bunny ear headband, while keeping the person’s original face, hairstyle, pose, and body proportions unchanged", "description": "把角色衣服换成兔女郎风格"},

    "泳装化": {"prompt": "Replace the outfit with a cute frilled white bikini swimsuit: a ruffled bikini top with small shoulder straps, a matching ruffled bikini bottom, paired with simple white sandals, beach accessories like sunglasses on the head, and a casual summer vibe, while keeping the person’s original face, hairstyle, pose, and body proportions unchanged", "description":"把角色衣服换成普通泳装"},

    "高叉泳装化": {"prompt": "Replace the outfit with a stylish beach swimsuit: a black halter-neck one-piece swimsuit with a deep neckline, paired with a sheer floral-patterned cover-up, gold hoop earrings, layered necklaces, sunglasses, and simple sandals, while keeping the person’s original face, hairstyle, pose, and body proportions unchanged", "description": "把角色衣服换成高叉泳装风格"},

    "穿龙袍": {"prompt": "Replace the outfit with a traditional imperial robe inspired by the Tang Dynasty, as shown in the reference image. The robe should be **golden-yellow silk**, featuring intricate **dragon embroidery** on the chest, sleeves, and lower part of the gown. The central design should display a **large, circular dragon and phoenix motif**, with additional **cloud and water designs** on the edges. The robe should have a **golden belt** with symbolic buckles, and the character should wear a **black imperial cap** with a decorative jewel. Keep the character's original pose, face, and background intact, ensuring the robe fits naturally with their body proportions. Ensure the regal and authoritative appearance of the emperor is reflected in their posture and facial expression.", "description": "穿龙袍"},

    "穿和服": {"prompt": "Replace the character's current outfit with the **traditional dark blue kimono** featured in the reference image. The kimono should have **intricate floral patterns** along the sleeves and hem, with a **purple sash** around the waist. Add a **small decorative pouch** held by the character in the right hand, as seen in the reference image. The hairstyle and facial features of the character should remain the same. Keep the posture, expression, and background unchanged, maintaining the serene and elegant aura of the original character. The character should have the same **slender figure**, with the kimono fitting naturally around the body.", "description": "ba佳代子款"},

    "拿菜刀": {"prompt": "((override original expression and pose)), half-body front view, head slightly lowered; ((half-lidded tired eyes)), ((closed neutral mouth)), soft gradient shadow under bangs; ((both hands together at chest level holding the handle of a stainless kitchen knife)), blade pointing downward away from face/body; keep identity, outfit, colors, style and background unchanged; clean anime rendering", "description": "拿菜刀"},

    "打歌服1": {"prompt": "Replace only the subject’s clothing with the exact outfit from the reference image: a gothic-lolita purple-and-black dress with layered ruffles and gold trim, big front ribbon, off-shoulder top with long dark gloves, thigh-high navy boots with gold lace, and a black rose/feather fascinator with a teal rose; keep the subject’s identity, face, hair color and style, skin tone, body proportions, pose, lighting, and background unchanged—no extra accessories, correct anatomy, high-detail.", "description": "ykn款"},

    "打歌服2": {"prompt": "Replace only the subject’s clothing with the reference outfit—white idol sailor dress with layered frilled skirt, oversized orange chest bow, one single blue gingham necktie centered under the bow (exactly one tie only), white thigh-highs with blue-gingham bows, optional matching headband—keep the subject’s identity, face, hair, pose, lighting, and background unchanged; no duplicate ties, no extra ribbons/straps around the neck or chest, no all-over gingham fabric.", "description": "鹭澤有里栖/咲"},

    "侦探服1": {"prompt": "Replace the outfit with a steampunk-school uniform like the reference: black-brown cropped jacket with epaulets, studs and gold piping, worn over a white long-sleeve blouse (button cuffs); single red ribbon necktie with a gold gear-like brooch at the collar; grey pleated skirt with fine vertical stripes and a gold hem, short black ruffle underskirt; leather utility belt on the waist with a side pouch/pen tools on the right hip; thigh-high black stockings (right leg has a small garter/strap detail); knee-high black boots with gold accents; one pair of aviator goggles resting on the headband; optional small enamel pins on the lapel—do not change the person’s face, hair color/style, body proportions, pose or background; keep anime rendering and natural fabric folds; no extra accessories besides those listed.", "description": "永雏塔菲"},

    "侦探服2": {"prompt": "#bnn Replace the outfit with a blue-plaid detective school set: white blouse with pointed collar and cuffs, sky-blue front vest panel with two decorative frog/loop toggles, a large navy bow with round gold brooch at the neck; short black capelet/poncho with wide sleeves and white piping; pleated blue tartan skirt over a black ruffle underskirt; small black cross-body satchel with scalloped flap; left leg in a black thigh-high stocking, right leg bare with a frilly ankle cuff and white bow; black Mary-Jane shoes with small white bows; optional black beret with blue-plaid ribbon and little white bird charm and an optional handheld magnifying glass prop; do not change the person’s face, hair color/style, body proportions, pose or background.", "description": "橘雪莉"},

    "脱衣":{"prompt" :"By dividing each wearable component into separate layers and neatly arranging them around the original character, the clothing of the character in the input image must be removed precisely. At the same time, the style and color of the underwear can be inferred and drawn based on the character's clothing, especially bras and underpants. Arrange all the removed clothes, including the underwear, around the character. The removed clothes must not appear on the character.Requirement: 1. No body parts (including limbs and hands and feet) can be missing. 2. Use a white or transparent background to keep it clear. 3. Add subtle dotted lines or arrows pointing to the original graphic to indicate the attachment points. 4. Maintain the original color/texture of each item. 5. Ensure that the disassembled clothes do not overlap with the original characters.6. Optional, add labels to complex components (for example, [Button Details], [zipper]). Style: Technical flat or explosive graphics, clean and simple.", "descrption": "仅保留内衣"},

    "抱枕化":{"prompt" :"When modifying the original image, the pose of the character should remain unchanged. Generate a full-body, realistic proportion, natural photo of the limbs with a white background. The photo should be placed on the front of a narrow and fluffy pillow for sale in reality, and the entire body of the character should be displayed on the surface of the pillow. The picture on the pillow should be stretched to fill the surface of the pillow. The picture must not appear anywhere else. The aspect ratio of the throw pillow should be 7:1, and the throw pillow should be the same size as a real adult female. The softness of the throw pillow should be depicted with numerous folds and variations in light and shadow. The throw pillow should be placed flat on the surface of the single bed. The placement of throw pillows should not extend beyond the bed. The bed is placed in a random single-person e-sports room. The room light should be behind the imaginary photographer, casting the shadow of the imaginary photographer holding a mobile phone to take a photo on the bed. The bed sheet pattern is inspired by reality, and there are random pillows, quilts and clothes on the bed. In the room, there should be an expensive ergonomic gaming chair and an e-sports table. There should be an RGB expensive desktop computer and a ribbonfish e-sports screen, as well as some miscellaneous items that are suitable for real gamers. The content on the computer screen is a random steam game library interface. On the computer case, there is a figurine that is highly similar to the character. There are some other random figurines on the table. It is in line with reality that there is a certain distance between the computer desk and the bed. The proportion of the room space is in line with reality.", "description": "制作输入图片抱枕"},

    "月饼化":{"prompt" :" Create an image of [character/subject] engraved as a mooncake pattern. Style: embossed, relief carving, baked golden texture, looks like it is imprinted on the surface of a traditional Chinese mooncake. Round cookie shape with scalloped edges. Keep the main subject clearly visible and centered. Lighting should highlight the baked golden-brown tones. Background: plain white or light to emphasize the mooncake.", "description": "将输入图片转换为月饼化风格"},
}

def get_help_text():
    """获取手办化插件的基本使用帮助"""
    return ("🎨 手办化插件使用帮助\n"
    "基本用法\n"
    "• 发送或引用一张图片，然后输入触发词即可进行图像风格转换\n"
    '• 示例：发送一张图片，然后输入 "手办化"\n\n'
    "特殊功能\n"
    "• @一个用户，可以使用TA的头像进行转换\n"
    "• 在触发词后添加额外说明，可以进一步自定义生成效果\n\n"
    "管理命令\n"
    "• #手办化签到 - 每日签到获取使用次数\n"
    "• #手办化查询次数 - 查询剩余使用次数\n"
    "• #手办化增加用户次数 - 管理员命令，增加用户使用次数\n"
    "• #手办化增加群组次数 - 管理员命令，增加群组使用次数\n"
    "• #手办化模型查看 - 管理员命令，查看可用图像模型\n"
    "• #手办化模型切换 <模型名> - 管理员命令，切换当前模型\n"
    "• #手办化添加key - 管理员命令，添加API Key\n"
    "• #手办化key列表 - 管理员命令，查看API Key列表\n"
    "• #手办化删除key - 管理员命令，删除API Key\n"
    "• #手办化使用本地模型 - 管理员命令，切换为本地 Z-Image 模型\n"
    "• #手办化使用云端模型 - 管理员命令，切换回云端 API\n"
    "• #手办化当前后端 - 查看当前使用的后端与 URL\n\n"
    "提示\n"
    "• bnn <提示词>：默认走 Gemini 系列模型；无图文生图，有图图生图\n"
    "• gpt <提示词>：默认走 GPT-Image 2.5；无图文生图，有图图生图\n"
    '• 输入 "手办化命令帮助" 查看所有可用的图像风格转换命令')

def get_command_help():
    """获取手办化插件的所有生图指令列表"""
    help_text = ["📋 手办化插件生图指令列表：\n"]
    # 按类别组织命令
    categories = {
        "手办化风格": [],
        "其他风格": []
    }

    # 将命令分类，只保留生图相关的命令
    admin_commands = ["手办化帮助", "手办化命令帮助", "手办化查询次数", "手办化签到", "手办化添加key", "手办化key列表", "手办化删除key", "手办化增加用户次数", "手办化增加群组次数"]

    for cmd, info in prompt_map.items():
        if cmd in admin_commands or cmd in {"bnn", "gpt"}:
            continue

        desc = info.get("description", "无描述") if isinstance(info, dict) else str(info)

        if cmd.startswith("手办化"):
            categories["手办化风格"].append(f"{cmd} - {desc}")
        else:
            categories["其他风格"].append(f"{cmd} - {desc}")

    # 添加分类标题和命令
    # 不使用sorted，保持prompt_map中的原始顺序
    for category, cmds in categories.items():
        if cmds:  # 只添加有命令的分类
            help_text.append(f"\n{category}：")
            help_text.extend(cmds)

    # 添加自定义提示词命令
    help_text.append("\n\n💡 自定义提示词：")
    help_text.append("bnn <自定义提示词> - 默认走 Gemini 系列模型；无图文生图，有图图生图")
    help_text.append("gpt <自定义提示词> - 默认走 GPT-Image 2.5；无图文生图，有图图生图")
    help_text.append("bna <自定义提示词> - 无需图片，直接使用自定义提示词生成图像")

    return "\n".join(help_text)
