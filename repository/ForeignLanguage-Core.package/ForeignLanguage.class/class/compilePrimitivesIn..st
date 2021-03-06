initialize-release
compilePrimitivesIn: newClass
	| primitiveSelectors |
	self flag: #instanceSidePrimitives.
	primitiveSelectors := ForeignLanguage class organization listAtCategoryNamed: 'system primitives'.
	primitiveSelectors do: [:selector | | template |
		template := (ForeignLanguage class >> selector) getSource asString.
		newClass class compile: (
			template copyReplaceAll: '{ForeignLanguage}' with: newClass name)]